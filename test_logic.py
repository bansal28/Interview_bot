import datetime
import unittest

import bot
import config
import db


class LogicTests(unittest.TestCase):
    def setUp(self):
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        self.old_db_path = config.DB_PATH
        config.DB_PATH = ":memory:"
        db.init_db()

    def tearDown(self):
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self.old_db_path

    def test_deadline_text_accepts_naive_timestamp(self):
        started = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
        ).replace(tzinfo=None)
        text = bot._deadline_text(started.isoformat(), 1)
        self.assertIn("left", text)

    def test_deadline_text_ignores_invalid_timestamp(self):
        self.assertEqual(bot._deadline_text("not-a-timestamp", 1), "")

    def test_upsert_user_repairs_legacy_null_defaults(self):
        conn = db._c()
        conn.execute(
            """INSERT INTO users
               (chat_id, tz, active_start, active_end, summary_hour, enabled,
                current_level, level_started_at, created_at)
               VALUES (?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)""",
            (123,),
        )
        conn.commit()

        user = db.upsert_user(123)

        self.assertEqual(user["tz"], config.DEFAULT_TZ)
        self.assertEqual(user["active_start"], config.DEFAULT_ACTIVE_START)
        self.assertEqual(user["active_end"], config.DEFAULT_ACTIVE_END)
        self.assertEqual(user["summary_hour"], config.DEFAULT_SUMMARY_HOUR)
        self.assertEqual(user["enabled"], 1)
        self.assertEqual(user["current_level"], 1)
        self.assertIsNotNone(user["level_started_at"])
        self.assertIsNotNone(user["created_at"])

    def test_add_answer_stores_interview_coaching_fields(self):
        db.add_answer(
            123,
            1,
            "Evaluation metrics",
            1,
            "It measures positives.",
            5.0,
            False,
            "Precision is TP / (TP + FP).",
            "Missing the denominator.",
            "wrong_formula",
            "Precision is the share of predicted positives that were actually positive.",
            "When would you prefer precision over recall?",
            "Do not confuse precision with accuracy.",
        )

        row = db._c().execute("SELECT * FROM answers WHERE chat_id=?", (123,)).fetchone()

        self.assertEqual(row["mistake_type"], "wrong_formula")
        self.assertIn("predicted positives", row["interview_answer"])
        self.assertIn("precision over recall", row["follow_up"])
        self.assertIn("accuracy", row["trap"])

    def test_mistake_stats_ignores_none_and_orders_by_frequency(self):
        db.add_answer(123, 1, "A", 1, "x", 4.0, False, "a", "", "vague_explanation")
        db.add_answer(123, 2, "A", 1, "x", 5.0, False, "a", "", "wrong_formula")
        db.add_answer(123, 3, "A", 1, "x", 5.0, False, "a", "", "vague_explanation")
        db.add_answer(123, 4, "A", 1, "x", 9.0, True, "a", "", "none")

        rows = db.mistake_stats(123)

        self.assertEqual(rows[0]["mistake_type"], "vague_explanation")
        self.assertEqual(rows[0]["n"], 2)
        self.assertEqual(rows[1]["mistake_type"], "wrong_formula")

    def test_user_agent_profile_fields_are_migrated_and_mutable(self):
        db.upsert_user(123)

        db.set_field(123, "prep_goal", "Prepare for an ML engineer interview")
        db.set_field(123, "target_role", "ML Engineer")
        db.set_field(123, "interview_date", "2026-07-15")

        user = db.get_user(123)

        self.assertEqual(user["prep_goal"], "Prepare for an ML engineer interview")
        self.assertEqual(user["target_role"], "ML Engineer")
        self.assertEqual(user["interview_date"], "2026-07-15")

    def test_plan_storage_and_agent_action_log(self):
        db.upsert_user(123)
        plan = {
            "summary": "Focus on weak ML fundamentals.",
            "priorities": ["metrics", "regularization"],
            "today": ["answer five flashcards"],
            "this_week": ["run one mock interview"],
            "success_metric": "Average at least 8/10.",
        }

        db.save_plan(123, plan)
        db.log_agent_action(123, "make_plan", "Generated a personalized plan.")

        user = db.get_user(123)
        events = db.recent_agent_events(123)

        self.assertEqual(db.get_plan(user), plan)
        self.assertIsNotNone(user["plan_updated_at"])
        self.assertEqual(user["last_agent_action"], "make_plan")
        self.assertEqual(events[0]["action"], "make_plan")

    def test_manual_questions_do_not_consume_automatic_daily_quota(self):
        db.upsert_user(123)

        db.add_question(123, "A", "easy", 1, "manual?", source="manual")
        db.add_question(123, "A", "easy", 1, "scheduled?", source="scheduled")
        db.add_question(123, "A", "easy", 1, "followup?", source="followup")

        self.assertEqual(db.questions_asked_today(123), 2)

    def test_next_question_and_optional_followup_fields_are_mutable(self):
        db.upsert_user(123)

        db.set_field(123, "next_question_at", "2026-07-15T10:00:00+00:00")
        db.set_field(123, "followup_question", "Why does precision matter?")
        db.set_field(123, "followup_topic", "Evaluation metrics")
        db.set_field(123, "followup_level", 2)

        user = db.get_user(123)

        self.assertEqual(user["next_question_at"], "2026-07-15T10:00:00+00:00")
        self.assertEqual(user["followup_question"], "Why does precision matter?")
        self.assertEqual(user["followup_topic"], "Evaluation metrics")
        self.assertEqual(user["followup_level"], 2)


if __name__ == "__main__":
    unittest.main()
