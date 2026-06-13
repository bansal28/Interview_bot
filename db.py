"""SQLite persistence layer.

Single low-traffic bot, one asyncio loop -> a single connection is fine.
Tracks per-user level progress in addition to questions/answers.
"""
import datetime
import json
import sqlite3

import config

_conn = None


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _c() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL;")
    return _conn


def _ensure_column(table: str, name: str, decl: str):
    cols = {r["name"] for r in _c().execute(f"PRAGMA table_info({table})").fetchall()}
    if name not in cols:
        _c().execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def init_db() -> None:
    c = _c()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            chat_id          INTEGER PRIMARY KEY,
            tz               TEXT,
            active_start     INTEGER,
            active_end       INTEGER,
            summary_hour     INTEGER,
            enabled          INTEGER DEFAULT 1,
            pending_qid      INTEGER,
            current_level    INTEGER DEFAULT 1,
            level_started_at TEXT,
            prep_goal        TEXT,
            target_role      TEXT,
            interview_date   TEXT,
            agent_plan       TEXT,
            plan_updated_at  TEXT,
            last_agent_action TEXT,
            next_question_at TEXT,
            followup_question TEXT,
            followup_topic   TEXT,
            followup_level   INTEGER,
            created_at       TEXT
        );
        CREATE TABLE IF NOT EXISTS questions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id    INTEGER,
            topic      TEXT,
            difficulty TEXT,
            level      INTEGER,
            question   TEXT,
            source     TEXT,
            asked_at   TEXT,
            answered   INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS agent_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id     INTEGER,
            action      TEXT,
            reason      TEXT,
            created_at  TEXT
        );
        CREATE TABLE IF NOT EXISTS answers (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id      INTEGER,
            qid          INTEGER,
            topic        TEXT,
            level        INTEGER,
            transcript   TEXT,
            score        REAL,
            correct      INTEGER,
            answer       TEXT,
            note         TEXT,
            mistake_type TEXT,
            interview_answer TEXT,
            follow_up    TEXT,
            trap         TEXT,
            created_at   TEXT
        );
        """
    )
    # Migrations for older databases.
    _ensure_column("users", "current_level", "INTEGER DEFAULT 1")
    _ensure_column("users", "level_started_at", "TEXT")
    _ensure_column("users", "prep_goal", "TEXT")
    _ensure_column("users", "target_role", "TEXT")
    _ensure_column("users", "interview_date", "TEXT")
    _ensure_column("users", "agent_plan", "TEXT")
    _ensure_column("users", "plan_updated_at", "TEXT")
    _ensure_column("users", "last_agent_action", "TEXT")
    _ensure_column("users", "next_question_at", "TEXT")
    _ensure_column("users", "followup_question", "TEXT")
    _ensure_column("users", "followup_topic", "TEXT")
    _ensure_column("users", "followup_level", "INTEGER")
    _ensure_column("questions", "level", "INTEGER")
    _ensure_column("questions", "source", "TEXT")
    _ensure_column("answers", "level", "INTEGER")
    _ensure_column("answers", "correct", "INTEGER")
    _ensure_column("answers", "answer", "TEXT")
    _ensure_column("answers", "note", "TEXT")
    _ensure_column("answers", "mistake_type", "TEXT")
    _ensure_column("answers", "interview_answer", "TEXT")
    _ensure_column("answers", "follow_up", "TEXT")
    _ensure_column("answers", "trap", "TEXT")
    c.commit()


# --- users ----------------------------------------------------------------
_ALLOWED_FIELDS = {
    "tz", "active_start", "active_end", "summary_hour",
    "enabled", "pending_qid", "current_level", "level_started_at",
    "prep_goal", "target_role", "interview_date", "agent_plan",
    "plan_updated_at", "last_agent_action", "next_question_at",
    "followup_question", "followup_topic", "followup_level",
}


def upsert_user(chat_id: int) -> sqlite3.Row:
    c = _c()
    if c.execute("SELECT 1 FROM users WHERE chat_id=?", (chat_id,)).fetchone() is None:
        c.execute(
            """INSERT INTO users
               (chat_id, tz, active_start, active_end, summary_hour, enabled,
                current_level, level_started_at, created_at)
               VALUES (?,?,?,?,?,1,1,?,?)""",
            (chat_id, config.DEFAULT_TZ, config.DEFAULT_ACTIVE_START,
             config.DEFAULT_ACTIVE_END, config.DEFAULT_SUMMARY_HOUR, _now(), _now()),
        )
        c.commit()
    else:
        c.execute(
            """UPDATE users
               SET tz=COALESCE(tz, ?),
                   active_start=COALESCE(active_start, ?),
                   active_end=COALESCE(active_end, ?),
                   summary_hour=COALESCE(summary_hour, ?),
                   enabled=COALESCE(enabled, 1),
                   current_level=COALESCE(current_level, 1),
                   level_started_at=COALESCE(level_started_at, ?),
                   created_at=COALESCE(created_at, ?)
               WHERE chat_id=?""",
            (config.DEFAULT_TZ, config.DEFAULT_ACTIVE_START,
             config.DEFAULT_ACTIVE_END, config.DEFAULT_SUMMARY_HOUR,
             _now(), _now(), chat_id),
        )
        c.commit()
    return get_user(chat_id)


def get_user(chat_id: int):
    return _c().execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()


def set_field(chat_id: int, field: str, value) -> None:
    if field not in _ALLOWED_FIELDS:
        raise ValueError(f"illegal field: {field}")
    c = _c()
    c.execute(f"UPDATE users SET {field}=? WHERE chat_id=?", (value, chat_id))
    c.commit()


def all_enabled_users():
    return _c().execute("SELECT * FROM users WHERE enabled=1").fetchall()


def save_plan(chat_id: int, plan: dict) -> None:
    c = _c()
    c.execute(
        """UPDATE users
           SET agent_plan=?, plan_updated_at=?
           WHERE chat_id=?""",
        (json.dumps(plan, ensure_ascii=False), _now(), chat_id),
    )
    c.commit()


def get_plan(user) -> dict:
    if not user or not user["agent_plan"]:
        return {}
    try:
        data = json.loads(user["agent_plan"])
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def log_agent_action(chat_id: int, action: str, reason: str = "") -> None:
    c = _c()
    c.execute(
        "INSERT INTO agent_events (chat_id, action, reason, created_at) VALUES (?,?,?,?)",
        (chat_id, action, reason, _now()),
    )
    c.execute(
        "UPDATE users SET last_agent_action=? WHERE chat_id=?",
        (action, chat_id),
    )
    c.commit()


def recent_agent_events(chat_id: int, limit: int = 5):
    return _c().execute(
        """SELECT action, reason, created_at
           FROM agent_events
           WHERE chat_id=?
           ORDER BY id DESC
           LIMIT ?""",
        (chat_id, limit),
    ).fetchall()


# --- questions ------------------------------------------------------------
def add_question(chat_id, topic, difficulty, level, question, source="scheduled") -> int:
    c = _c()
    cur = c.execute(
        """INSERT INTO questions
           (chat_id, topic, difficulty, level, question, source, asked_at, answered)
           VALUES (?,?,?,?,?,?,?,0)""",
        (chat_id, topic, difficulty, level, question, source, _now()),
    )
    c.commit()
    return cur.lastrowid


def get_question(qid: int):
    return _c().execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()


def mark_answered(qid: int) -> None:
    c = _c()
    c.execute("UPDATE questions SET answered=1 WHERE id=?", (qid,))
    c.commit()


def recent_question_texts(chat_id: int, limit: int = 20):
    rows = _c().execute(
        "SELECT question FROM questions WHERE chat_id=? ORDER BY id DESC LIMIT ?",
        (chat_id, limit),
    ).fetchall()
    return [r["question"] for r in rows]


def _start_of_today_utc(chat_id: int) -> str:
    user = get_user(chat_id)
    tz = config.tz_of(user["tz"]) if user else config.tz_of(config.DEFAULT_TZ)
    now_local = datetime.datetime.now(tz)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(datetime.timezone.utc).isoformat()


def questions_asked_today(chat_id: int) -> int:
    row = _c().execute(
        """SELECT COUNT(*) AS n
           FROM questions
           WHERE chat_id=?
             AND asked_at>=?
             AND COALESCE(source, 'scheduled') != 'manual'""",
        (chat_id, _start_of_today_utc(chat_id)),
    ).fetchone()
    return row["n"]


# --- answers / stats ------------------------------------------------------
def add_answer(chat_id, qid, topic, level, transcript, score, correct, answer, note,
               mistake_type="none", interview_answer="", follow_up="", trap="") -> None:
    c = _c()
    c.execute(
        """INSERT INTO answers
           (chat_id, qid, topic, level, transcript, score, correct, answer, note,
            mistake_type, interview_answer, follow_up, trap, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (chat_id, qid, topic, level, transcript, score,
         1 if correct else 0, answer, note, mistake_type,
         interview_answer, follow_up, trap, _now()),
    )
    c.commit()


def answers_at_level(chat_id: int, level: int) -> int:
    row = _c().execute(
        "SELECT COUNT(*) AS n FROM answers WHERE chat_id=? AND level=?",
        (chat_id, level),
    ).fetchone()
    return row["n"]


def level_avg(chat_id: int, level: int):
    row = _c().execute(
        "SELECT AVG(score) AS avg FROM answers WHERE chat_id=? AND level=?",
        (chat_id, level),
    ).fetchone()
    return row["avg"]


def topic_avg(chat_id: int, topic: str):
    row = _c().execute(
        "SELECT AVG(score) AS avg FROM answers WHERE chat_id=? AND topic=?",
        (chat_id, topic),
    ).fetchone()
    return row["avg"]


def topic_stats(chat_id: int):
    """Returns rows with keys: topic, avg, n."""
    return _c().execute(
        """SELECT topic, AVG(score) AS avg, COUNT(*) AS n
           FROM answers WHERE chat_id=? GROUP BY topic""",
        (chat_id,),
    ).fetchall()


def overall_stats(chat_id: int):
    row = _c().execute(
        "SELECT AVG(score) AS avg, COUNT(*) AS n FROM answers WHERE chat_id=?",
        (chat_id,),
    ).fetchone()
    return (row["avg"] or 0.0, row["n"] or 0)


def mistake_stats(chat_id: int, limit: int = 3):
    """Returns the most common non-empty weakness patterns."""
    return _c().execute(
        """SELECT mistake_type, COUNT(*) AS n
           FROM answers
           WHERE chat_id=?
             AND mistake_type IS NOT NULL
             AND mistake_type NOT IN ('', 'none')
           GROUP BY mistake_type
           ORDER BY n DESC, mistake_type ASC
           LIMIT ?""",
        (chat_id, limit),
    ).fetchall()


def recent_answers(chat_id: int, limit: int = 5):
    return _c().execute(
        """SELECT q.question, a.topic, a.score, a.mistake_type, a.follow_up, a.created_at
           FROM answers a
           LEFT JOIN questions q ON q.id = a.qid
           WHERE a.chat_id=?
           ORDER BY a.id DESC
           LIMIT ?""",
        (chat_id, limit),
    ).fetchall()


def answered_today(chat_id: int) -> int:
    row = _c().execute(
        "SELECT COUNT(*) AS n FROM answers WHERE chat_id=? AND created_at>=?",
        (chat_id, _start_of_today_utc(chat_id)),
    ).fetchone()
    return row["n"]
