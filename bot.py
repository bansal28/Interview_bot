"""ML/AI interview coach Telegram bot — flashcard + levels edition.

Run:  python bot.py   (after filling .env)

Short, atomic flashcard questions drip through the day. You answer by voice; the
bot grades it, gives the short correct answer, and tracks progress. Questions are
organized into difficulty LEVELS, each with a question count and a target deadline.
Clear a level's questions to level up. Difficulty rises each level.
"""
import asyncio
import datetime
import logging
import math
import os
import random
import tempfile
from datetime import time as dtime, timedelta

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (Application, CommandHandler, ContextTypes,
                          MessageHandler, filters)

import ai
import config
import db

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("coach")


def _utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# =========================================================================
# Levels helpers
# =========================================================================
def daily_target_for(user) -> int:
    """How many questions per day to hit the level's deadline."""
    lv = config.level_def(user["current_level"])
    if not lv:
        return config.REVIEW_PACE
    return max(1, math.ceil(lv["questions"] / lv["days"]))


def _deadline_text(started_iso: str, days: int) -> str:
    if not started_iso:
        return ""
    try:
        started = datetime.datetime.fromisoformat(started_iso)
    except ValueError:
        log.warning("invalid level_started_at timestamp: %r", started_iso)
        return ""
    if started.tzinfo is None:
        started = started.replace(tzinfo=datetime.timezone.utc)
    deadline = started + timedelta(days=days)
    delta = deadline - datetime.datetime.now(datetime.timezone.utc)
    if delta.total_seconds() <= 0:
        return "⏰ past target date — keep going!"
    d, h = delta.days, delta.seconds // 3600
    return f"⏳ {d}d {h}h left" if d >= 1 else f"⏳ {h}h left"


def _progress_bar(done: int, target: int, cells: int = 10) -> str:
    filled = max(0, min(cells, round(done / target * cells))) if target else 0
    return "🟩" * filled + "⬜" * (cells - filled)


def _humanize_label(value: str) -> str:
    return (value or "").replace("_", " ").strip().capitalize()


def _format_next_question(user) -> str:
    if not user or not user["next_question_at"]:
        return "No automatic question is currently scheduled. Use /resume to restart the drip."
    try:
        next_at = datetime.datetime.fromisoformat(user["next_question_at"])
    except ValueError:
        return "The next scheduled time is invalid. Use /resume to rebuild the schedule."
    if next_at.tzinfo is None:
        next_at = next_at.replace(tzinfo=datetime.timezone.utc)
    tz = config.tz_of(user["tz"])
    local = next_at.astimezone(tz)
    delta = next_at - datetime.datetime.now(datetime.timezone.utc)
    if delta.total_seconds() <= 0:
        return "The next automatic question is due now. It should arrive shortly."
    minutes = max(1, math.ceil(delta.total_seconds() / 60))
    return f"Next automatic question: {local:%Y-%m-%d %H:%M %Z} (~{minutes} min)."


# =========================================================================
# Topic selection (weakness-weighted)
# =========================================================================
def pick_topic(chat_id: int, preferred: str = "") -> str:
    if preferred:
        for topic in config.TOPICS:
            if preferred.lower() in topic.lower() or topic.lower() in preferred.lower():
                return topic
    stats = {r["topic"]: (r["avg"], r["n"]) for r in db.topic_stats(chat_id)}
    weights = []
    for t in config.TOPICS:
        avg, n = stats.get(t, (None, 0))
        w = 6.0 if avg is None else max(0.5, 10.0 - avg)
        if n < 2:
            w += 2.0
        weights.append(w)
    if random.random() < 0.15:
        return random.choice(config.TOPICS)
    return random.choices(config.TOPICS, weights=weights, k=1)[0]


# =========================================================================
# Sending questions
# =========================================================================
async def send_question(context: ContextTypes.DEFAULT_TYPE, chat_id: int, topic_override: str = "",
                        source: str = "scheduled", question_override: str = ""):
    user = db.get_user(chat_id)
    level = user["current_level"]
    lv = config.level_def(level)
    topic = pick_topic(chat_id, topic_override)

    if lv:
        difficulty = lv["difficulty"]
    else:  # review mode: scale difficulty by mastery of the chosen topic
        avg = db.topic_avg(chat_id, topic)
        difficulty = ("easy (concise answer)" if avg is None or avg < 4.5
                      else "hard (concise answer)" if avg >= 7.5
                      else "medium (concise answer)")

    if question_override:
        question = question_override
    else:
        avoid = db.recent_question_texts(chat_id, limit=20)
        try:
            question = await asyncio.to_thread(ai.generate_question, topic, difficulty, avoid)
        except Exception:
            log.exception("question generation failed for %s", chat_id)
            return
    qid = db.add_question(chat_id, topic, difficulty, level, question, source=source)
    db.set_field(chat_id, "pending_qid", qid)

    if lv:
        done = db.answers_at_level(chat_id, level)
        header = f"🎴 Level {level} · {lv['name']} · {done + 1}/{lv['questions']}"
    else:
        header = "🔁 Review"

    await context.bot.send_message(
        chat_id,
        f"{header}\n📚 {topic}\n\n{question}\n\n"
        f"Reply with a quick voice note — a word or one sentence.",
    )


# =========================================================================
# Agent state, planning, and action execution
# =========================================================================
def _rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


def _agent_state(chat_id: int) -> dict:
    user = db.upsert_user(chat_id)
    lv = config.level_def(user["current_level"])
    done = db.answers_at_level(chat_id, user["current_level"]) if lv else 0
    overall_avg, total = db.overall_stats(chat_id)
    weak_rows = sorted(db.topic_stats(chat_id), key=lambda r: r["avg"])[:3]
    return {
        "prep_goal": user["prep_goal"] or "",
        "target_role": user["target_role"] or "",
        "interview_date": user["interview_date"] or "",
        "current_level": user["current_level"],
        "level_name": lv["name"] if lv else "Review mode",
        "level_progress": f"{done}/{lv['questions']}" if lv else "all levels cleared",
        "daily_target": daily_target_for(user),
        "answered_today": db.answered_today(chat_id),
        "overall_average": round(overall_avg, 1),
        "total_answers": total,
        "weak_topics": _rows_to_dicts(weak_rows),
        "mistake_patterns": _rows_to_dicts(db.mistake_stats(chat_id)),
        "recent_answers": _rows_to_dicts(db.recent_answers(chat_id)),
        "has_pending_question": bool(user["pending_qid"]),
        "has_plan": bool(user["agent_plan"]),
        "last_agent_action": user["last_agent_action"] or "",
    }


def _format_list(items: list[str]) -> list[str]:
    return [f"  • {item}" for item in items if item]


def _format_plan(plan: dict) -> str:
    if not plan:
        return "No study plan yet. Use /plan to generate one."
    lines = ["🧭 Agent study plan"]
    if plan.get("summary"):
        lines.append(plan["summary"])
    if plan.get("priorities"):
        lines.append("\nPriorities:")
        lines += _format_list(plan["priorities"])
    if plan.get("today"):
        lines.append("\nToday:")
        lines += _format_list(plan["today"])
    if plan.get("this_week"):
        lines.append("\nThis week:")
        lines += _format_list(plan["this_week"])
    if plan.get("success_metric"):
        lines.append(f"\nSuccess metric: {plan['success_metric']}")
    return "\n".join(lines)


async def _make_and_send_plan(update: Update, chat_id: int, log_action: bool = True):
    text = await _build_plan_text(chat_id, log_action=log_action)
    await update.message.reply_text(text)


async def _build_plan_text(chat_id: int, log_action: bool = True) -> str:
    state = _agent_state(chat_id)
    try:
        plan = await asyncio.to_thread(ai.build_study_plan, state)
    except Exception:
        log.exception("agent plan failed for %s", chat_id)
        return "I couldn't build the plan right now — try again in a moment."
    db.save_plan(chat_id, plan)
    if log_action:
        db.log_agent_action(chat_id, "make_plan", "Generated a personalized prep plan.")
    return _format_plan(plan)


def _progress_text(chat_id: int) -> str:
    u = db.upsert_user(chat_id)
    level = u["current_level"]
    lv = config.level_def(level)
    if not lv:
        avg, total = db.overall_stats(chat_id)
        return (
            f"🔁 Review mode — all {config.LEVEL_COUNT} levels cleared.\n"
            f"Overall: {avg:.1f}/10 across {total} answers. See /stats for topics."
        )
    done = db.answers_at_level(chat_id, level)
    avg = db.level_avg(chat_id, level)
    bar = _progress_bar(done, lv["questions"])
    dl = _deadline_text(u["level_started_at"], lv["days"])
    avg_line = f"Level average: {avg:.1f}/10\n" if avg is not None else ""
    return (
        f"🎮 Level {level}/{config.LEVEL_COUNT}: {lv['name']}\n"
        f"Progress: {done}/{lv['questions']} {bar}\n"
        f"{avg_line}"
        f"{dl}\n"
        f"Pace: ~{daily_target_for(u)} cards/day"
    )


async def _decide_agent_action(chat_id: int) -> dict:
    state = _agent_state(chat_id)
    try:
        return await asyncio.to_thread(ai.decide_next_action, state)
    except Exception:
        log.exception("agent decision failed for %s", chat_id)
        return {
            "action": "make_plan" if not state["has_plan"] else "ask_flashcard",
            "reason": "Fallback decision after planner error.",
            "message": "",
            "focus_topic": "",
        }


async def _run_agent_action(update: Update, context: ContextTypes.DEFAULT_TYPE, question_source: str = "manual"):
    chat_id = update.effective_chat.id
    decision = await _decide_agent_action(chat_id)
    action = decision["action"]
    db.log_agent_action(chat_id, action, decision.get("reason", ""))
    if decision.get("message"):
        await update.message.reply_text(f"🤖 {decision['message']}")

    if action == "make_plan":
        await _make_and_send_plan(update, chat_id, log_action=False)
    elif action == "review_weakness":
        await send_stats(context.bot, chat_id)
    elif action == "progress_review":
        await cmd_progress(update, context)
        await send_stats(context.bot, chat_id)
    else:
        await send_question(context, chat_id, decision.get("focus_topic", ""), source=question_source)


async def _run_scheduled_agent_action(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    decision = await _decide_agent_action(chat_id)
    action = decision["action"]
    db.log_agent_action(chat_id, action, decision.get("reason", ""))
    if decision.get("message"):
        await context.bot.send_message(chat_id, f"🤖 {decision['message']}")

    if action == "make_plan":
        await context.bot.send_message(chat_id, await _build_plan_text(chat_id, log_action=False))
    elif action == "review_weakness":
        await send_stats(context.bot, chat_id)
    elif action == "progress_review":
        await context.bot.send_message(chat_id, _progress_text(chat_id))
    else:
        await send_question(context, chat_id, decision.get("focus_topic", ""), source="scheduled")


# =========================================================================
# Scheduling (paced to the level deadline, jittered, clamped to active hours)
# =========================================================================
def _clear_jobs(job_queue, name: str):
    for j in job_queue.get_jobs_by_name(name):
        j.schedule_removal()


def _at_window_start(day: datetime.datetime, start_h: int) -> datetime.datetime:
    base = day.replace(hour=start_h, minute=0, second=0, microsecond=0)
    return base + timedelta(minutes=random.randint(0, 15))


def _seconds_until_window_start(user) -> float:
    tz = config.tz_of(user["tz"])
    now = datetime.datetime.now(tz)
    start_h = user["active_start"]
    day = now if now.hour < start_h else now + timedelta(days=1)
    return max(60.0, (_at_window_start(day, start_h) - now).total_seconds())


def _seconds_until_next(user) -> float:
    tz = config.tz_of(user["tz"])
    now = datetime.datetime.now(tz)
    start_h, end_h = user["active_start"], user["active_end"]
    window_min = max(1, (end_h - start_h) * 60)
    spacing = window_min / daily_target_for(user)
    delay_min = max(5.0, spacing * random.uniform(0.6, 1.4))
    target = now + timedelta(minutes=delay_min)
    if target.hour < start_h:
        target = _at_window_start(target, start_h)
    elif target.hour >= end_h:
        target = _at_window_start(target + timedelta(days=1), start_h)
    return max(60.0, (target - now).total_seconds())


def schedule_next(job_queue, chat_id: int):
    user = db.get_user(chat_id)
    if not user or not user["enabled"]:
        return
    _clear_jobs(job_queue, f"q-{chat_id}")
    if db.questions_asked_today(chat_id) >= daily_target_for(user):
        delay = _seconds_until_window_start(user)    # day's pace met; resume tomorrow
    else:
        delay = _seconds_until_next(user)
    next_at = datetime.datetime.now(datetime.timezone.utc) + timedelta(seconds=delay)
    db.set_field(chat_id, "next_question_at", next_at.isoformat())
    job_queue.run_once(job_send_question, when=delay, chat_id=chat_id, name=f"q-{chat_id}")
    log.info("next question for %s in ~%.0f min", chat_id, delay / 60)


def schedule_summary(job_queue, chat_id: int):
    user = db.get_user(chat_id)
    if not user:
        return
    tz = config.tz_of(user["tz"])
    when = dtime(hour=user["summary_hour"], minute=30, tzinfo=tz)
    _clear_jobs(job_queue, f"sum-{chat_id}")
    job_queue.run_daily(job_daily_summary, time=when, chat_id=chat_id, name=f"sum-{chat_id}")


async def job_send_question(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    user = db.get_user(chat_id)
    if not user or not user["enabled"]:
        return
    if user["pending_qid"]:                          # don't stack — nudge instead
        q = db.get_question(user["pending_qid"])
        if q and not q["answered"]:
            await context.bot.send_message(
                chat_id,
                f"⏰ Reminder — still waiting on this one:\n\n{q['question']}\n\n"
                f"Reply with a quick voice note (or text).",
            )
            schedule_next(context.job_queue, chat_id)
            return
    if db.questions_asked_today(chat_id) >= daily_target_for(user):
        schedule_next(context.job_queue, chat_id)
        return
    if user["prep_goal"]:
        await _run_scheduled_agent_action(context, chat_id)
    else:
        await send_question(context, chat_id)
    schedule_next(context.job_queue, chat_id)


async def job_daily_summary(context: ContextTypes.DEFAULT_TYPE):
    await send_stats(context.bot, context.job.chat_id, daily=True)


# =========================================================================
# Stats / digest
# =========================================================================
async def send_stats(bot, chat_id: int, daily: bool = False):
    rows = db.topic_stats(chat_id)
    overall_avg, total = db.overall_stats(chat_id)
    if total == 0:
        await bot.send_message(chat_id, "No answers yet — use /ask to get your first card.")
        return

    by_score = sorted(rows, key=lambda r: r["avg"])
    weak = [r["topic"] for r in by_score[:3]]
    strong = sorted(rows, key=lambda r: -r["avg"])[:3]

    lines = []
    if daily:
        lines.append(f"🌙 Daily recap — {db.answered_today(chat_id)} card(s) answered today.\n")
    lines.append(f"📊 Overall: {overall_avg:.1f}/10 across {total} answers.")
    lines.append("\n🎯 Focus areas (weakest):")
    lines += [f"  • {r['topic']} — {r['avg']:.1f}/10 ({r['n']})" for r in by_score[:3]]
    lines.append("\n💪 Strong areas:")
    lines += [f"  • {r['topic']} — {r['avg']:.1f}/10 ({r['n']})" for r in strong]
    mistakes = db.mistake_stats(chat_id)
    if mistakes:
        lines.append("\n🔎 Most common answer issues:")
        lines += [f"  • {_humanize_label(r['mistake_type'])} — {r['n']}" for r in mistakes]
    await bot.send_message(chat_id, "\n".join(lines))

    try:
        tip = await asyncio.to_thread(ai.study_tip, weak)
        if tip:
            await bot.send_message(chat_id, f"📚 {tip}")
    except Exception:
        log.exception("study tip failed for %s", chat_id)


# =========================================================================
# Grading flow + level progression
# =========================================================================
async def grade_and_reply(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          q, transcript: str):
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    try:
        res = await asyncio.to_thread(ai.grade_answer, q["question"], q["topic"], transcript)
    except Exception:
        log.exception("grading failed for %s", chat_id)
        await update.message.reply_text("Something went wrong grading that — please try again.")
        return

    level = q["level"]
    db.add_answer(chat_id, q["id"], q["topic"], level, transcript,
                  res["score"], res["correct"], res["answer"], res["note"],
                  res["mistake_type"], res["interview_answer"],
                  res["follow_up"], res["trap"])
    db.mark_answered(q["id"])
    db.set_field(chat_id, "pending_qid", None)

    # Flashcard feedback — short.
    mark = "✅ Correct" if res["correct"] else "❌ Not quite"
    parts = [f"{mark} · {res['score']:.0f}/10", f"Answer: {res['answer']}"]
    if res["note"]:
        parts.append(res["note"])
    if res["mistake_type"] != "none":
        parts.append(f"Issue: {_humanize_label(res['mistake_type'])}")
    if res["interview_answer"]:
        parts.append(f"\nInterview-ready:\n{res['interview_answer']}")
    if res["trap"]:
        parts.append(f"\nWatch out:\n{res['trap']}")
    if res["follow_up"]:
        db.set_field(chat_id, "followup_question", res["follow_up"])
        db.set_field(chat_id, "followup_topic", q["topic"])
        db.set_field(chat_id, "followup_level", level)
        parts.append("\nOptional follow-up saved. Use /followup to try it, or /skipfollowup to ignore it.")
    await update.message.reply_text("\n".join(parts))

    # Level progression.
    lv = config.level_def(level)
    if lv and db.answers_at_level(chat_id, level) >= lv["questions"]:
        await _level_up(update, context, level)
    elif lv:
        done = db.answers_at_level(chat_id, level)
        user = db.get_user(chat_id)
        dl = _deadline_text(user["level_started_at"], lv["days"])
        bar = _progress_bar(done, lv["questions"])
        line = f"Level {level}: {done}/{lv['questions']} {bar}"
        if dl:
            line += f" · {dl}"
        await update.message.reply_text(line)


async def _level_up(update: Update, context: ContextTypes.DEFAULT_TYPE, level: int):
    chat_id = update.effective_chat.id
    avg = db.level_avg(chat_id, level) or 0.0
    new_level = level + 1
    db.set_field(chat_id, "current_level", new_level)
    db.set_field(chat_id, "level_started_at", _utcnow_iso())

    nlv = config.level_def(new_level)
    if nlv:
        msg = (
            f"🎉 Level {level} complete — average {avg:.1f}/10!\n\n"
            f"⬆️ Now entering Level {new_level}: {nlv['name']}\n"
            f"{nlv['questions']} cards · {nlv['days']} days · difficulty steps up."
        )
    else:
        msg = (
            f"🏆 You've cleared all {config.LEVEL_COUNT} levels — average {avg:.1f}/10!\n\n"
            "I'll keep sending mixed-difficulty cards focused on your weaker topics for "
            "review. Check /stats anytime."
        )
    await update.message.reply_text(msg)
    schedule_next(context.job_queue, chat_id)   # repace for the new level


# =========================================================================
# Handlers
# =========================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    u = db.upsert_user(chat_id)
    db.set_field(chat_id, "enabled", 1)
    if not u["level_started_at"]:
        db.set_field(chat_id, "level_started_at", _utcnow_iso())
    schedule_next(context.job_queue, chat_id)
    schedule_summary(context.job_queue, chat_id)

    lv = config.level_def(u["current_level"])
    intro_level = (f"You're on Level {u['current_level']}: {lv['name']} "
                   f"({lv['questions']} cards in {lv['days']} days)." if lv
                   else "You're in review mode — mixed cards on your weak spots.")
    await update.message.reply_text(
        "👋 I'm your ML/AI flashcard coach.\n\n"
        "Short questions drip through the day. Answer each with a quick voice note "
        "(a word or one sentence); I score it, classify weak spots, and give a "
        "more interview-ready answer. Cards are "
        f"grouped into {config.LEVEL_COUNT} levels — clear a level's cards to level up, "
        "and the difficulty rises each level.\n\n"
        f"{intro_level}\n\n"
        "Commands:\n"
        "/ask — an extra card right now\n"
        "/next — see when the next automatic card will arrive\n"
        "/followup — attempt the optional follow-up from your last answer\n"
        "/skipfollowup — clear the optional follow-up\n"
        "/progress — your current level & pace\n"
        "/levels — the full level map\n"
        "/stats — strong & weak topics\n"
        "/goal — set your interview prep goal\n"
        "/plan — generate an agent study plan\n"
        "/coach — let the agent choose the next action\n"
        "/profile — show the agent's user model\n"
        "/pause /resume — stop / restart\n"
        "/settings — schedule\n"
        "/sethours 9 21 — active window (24h)\n"
        "/settz Europe/London — timezone\n\n"
        "I'll send the first card now if there isn't one already open."
    )
    current = db.get_user(chat_id)
    if not current["pending_qid"] and db.questions_asked_today(chat_id) < daily_target_for(current):
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        if current["prep_goal"]:
            await _run_agent_action(update, context, question_source="scheduled")
        else:
            await send_question(context, chat_id, source="scheduled")


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db.upsert_user(chat_id)
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    await send_question(context, chat_id, source="manual")


async def cmd_progress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(_progress_text(update.effective_chat.id))


async def cmd_levels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    u = db.upsert_user(chat_id)
    cur = u["current_level"]
    lines = ["🗺️ Levels:"]
    for i, lv in enumerate(config.LEVELS, start=1):
        if cur > i:
            mark = "✅"
        elif cur == i:
            mark = "▶️"
        else:
            mark = "🔒"
        lines.append(f"{mark} L{i} {lv['name']} — {lv['questions']} cards / {lv['days']}d")
    await update.message.reply_text("\n".join(lines))


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_stats(context.bot, update.effective_chat.id)


async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = db.upsert_user(update.effective_chat.id)
    pending = ""
    if user["pending_qid"]:
        q = db.get_question(user["pending_qid"])
        if q and not q["answered"]:
            pending = "There is already an open card waiting for your answer.\n\n"
    await update.message.reply_text(pending + _format_next_question(user))


async def cmd_followup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = db.upsert_user(chat_id)
    if user["pending_qid"]:
        await update.message.reply_text("Finish the open card first, then use /followup.")
        return
    if not user["followup_question"]:
        await update.message.reply_text("No optional follow-up is saved right now.")
        return
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    await send_question(
        context,
        chat_id,
        topic_override=user["followup_topic"] or "",
        source="followup",
        question_override=user["followup_question"],
    )
    db.set_field(chat_id, "followup_question", None)
    db.set_field(chat_id, "followup_topic", None)
    db.set_field(chat_id, "followup_level", None)


async def cmd_skipfollowup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db.upsert_user(chat_id)
    db.set_field(chat_id, "followup_question", None)
    db.set_field(chat_id, "followup_topic", None)
    db.set_field(chat_id, "followup_level", None)
    await update.message.reply_text("Optional follow-up cleared.")


async def cmd_goal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db.upsert_user(chat_id)
    goal = " ".join(context.args).strip()
    if not goal:
        await update.message.reply_text(
            "Usage: /goal Prepare me for an ML Engineer interview in 30 days"
        )
        return
    db.set_field(chat_id, "prep_goal", goal)
    db.set_field(chat_id, "agent_plan", None)
    db.set_field(chat_id, "plan_updated_at", None)
    db.log_agent_action(chat_id, "set_goal", "User updated the prep goal.")
    await update.message.reply_text(
        "Goal saved. Use /plan to generate a study plan, or /coach to let the agent choose next."
    )


async def cmd_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db.upsert_user(chat_id)
    target = " ".join(context.args).strip()
    if not target:
        await update.message.reply_text("Usage: /target ML Engineer at a product company")
        return
    db.set_field(chat_id, "target_role", target)
    db.set_field(chat_id, "agent_plan", None)
    db.set_field(chat_id, "plan_updated_at", None)
    db.log_agent_action(chat_id, "set_target", "User updated the target role.")
    await update.message.reply_text("Target role saved. Use /plan to refresh your prep plan.")


async def cmd_interview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db.upsert_user(chat_id)
    date_text = " ".join(context.args).strip()
    if not date_text:
        await update.message.reply_text("Usage: /interview 2026-07-15")
        return
    db.set_field(chat_id, "interview_date", date_text)
    db.set_field(chat_id, "agent_plan", None)
    db.set_field(chat_id, "plan_updated_at", None)
    db.log_agent_action(chat_id, "set_interview_date", "User updated the interview date.")
    await update.message.reply_text("Interview date saved. Use /plan to refresh your prep plan.")


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = db.upsert_user(chat_id)
    state = _agent_state(chat_id)
    weak = [r["topic"] for r in state["weak_topics"]]
    issues = [_humanize_label(r["mistake_type"]) for r in state["mistake_patterns"]]
    lines = [
        "👤 Agent profile",
        f"Goal: {user['prep_goal'] or 'not set'}",
        f"Target: {user['target_role'] or 'not set'}",
        f"Interview date: {user['interview_date'] or 'not set'}",
        f"Level: {state['current_level']} · {state['level_name']} · {state['level_progress']}",
        f"Overall: {state['overall_average']}/10 across {state['total_answers']} answers",
        f"Plan: {'ready' if state['has_plan'] else 'not generated'}",
    ]
    if weak:
        lines.append("Weak topics: " + "; ".join(weak))
    if issues:
        lines.append("Recurring issues: " + "; ".join(issues))
    await update.message.reply_text("\n".join(lines))


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = db.upsert_user(chat_id)
    if not user["prep_goal"] and context.args:
        db.set_field(chat_id, "prep_goal", " ".join(context.args).strip())
    elif not user["prep_goal"]:
        await update.message.reply_text(
            "Set a goal first, for example:\n"
            "/goal Prepare me for an ML Engineer interview in 30 days"
        )
        return
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    await _make_and_send_plan(update, chat_id)


async def cmd_coach(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = db.upsert_user(chat_id)
    if not user["prep_goal"]:
        await update.message.reply_text(
            "Set your prep goal first:\n"
            "/goal Prepare me for an ML Engineer interview in 30 days"
        )
        return
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    await _run_agent_action(update, context)


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db.set_field(chat_id, "enabled", 0)
    db.set_field(chat_id, "next_question_at", None)
    _clear_jobs(context.job_queue, f"q-{chat_id}")
    await update.message.reply_text("⏸️ Paused. Use /resume anytime. You can still use /ask.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db.upsert_user(chat_id)
    db.set_field(chat_id, "enabled", 1)
    schedule_next(context.job_queue, chat_id)
    schedule_summary(context.job_queue, chat_id)
    await update.message.reply_text(
        "▶️ Resumed. Cards will arrive during your active hours.\n"
        f"{_format_next_question(db.get_user(chat_id))}"
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = db.upsert_user(update.effective_chat.id)
    lv = config.level_def(u["current_level"])
    level_line = (f"Level: {u['current_level']}/{config.LEVEL_COUNT} — {lv['name']}"
                  if lv else "Level: review mode (all cleared)")
    await update.message.reply_text(
        "⚙️ Your settings:\n"
        f"{level_line}\n"
        f"Pace: ~{daily_target_for(u)} cards/day\n"
        f"Timezone: {u['tz']}\n"
        f"Active hours: {u['active_start']:02d}:00–{u['active_end']:02d}:00\n"
        f"Daily recap: {u['summary_hour']:02d}:30\n"
        f"{_format_next_question(u)}\n"
        f"Goal: {u['prep_goal'] or 'not set'}\n"
        f"Status: {'on' if u['enabled'] else 'paused'}"
    )


async def cmd_sethours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        s, e = int(context.args[0]), int(context.args[1])
        assert 0 <= s < e <= 24
    except Exception:
        await update.message.reply_text("Usage: /sethours 9 21   (start hour, end hour, 24h)")
        return
    cid = update.effective_chat.id
    db.upsert_user(cid)
    db.set_field(cid, "active_start", s)
    db.set_field(cid, "active_end", e)
    schedule_next(context.job_queue, cid)
    await update.message.reply_text(
        f"Active hours set to {s:02d}:00–{e:02d}:00.\n"
        f"{_format_next_question(db.get_user(cid))}"
    )


async def cmd_settz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /settz Europe/London")
        return
    name = context.args[0]
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(name)
    except Exception:
        await update.message.reply_text("Unknown timezone. Use an IANA name like Europe/London or Asia/Kolkata.")
        return
    cid = update.effective_chat.id
    db.upsert_user(cid)
    db.set_field(cid, "tz", name)
    schedule_next(context.job_queue, cid)
    schedule_summary(context.job_queue, cid)
    await update.message.reply_text(
        f"Timezone set to {name}.\n{_format_next_question(db.get_user(cid))}"
    )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = db.upsert_user(chat_id)
    qid = user["pending_qid"]
    if not qid:
        await update.message.reply_text(
            "No open card right now. Use /ask to get one, then reply with your voice answer."
        )
        return
    q = db.get_question(qid)
    if not q or q["answered"]:
        db.set_field(chat_id, "pending_qid", None)
        await update.message.reply_text("That one's already done — use /ask for a new card.")
        return

    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    tg_file = await context.bot.get_file(update.message.voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        path = tmp.name
    await tg_file.download_to_drive(custom_path=path)
    try:
        transcript = await asyncio.to_thread(ai.transcribe, path)
    except Exception:
        log.exception("transcription failed for %s", chat_id)
        await update.message.reply_text("I couldn't transcribe that — please try recording again.")
        return
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    if not transcript.strip():
        await update.message.reply_text("I couldn't hear anything — try recording again.")
        return
    await grade_and_reply(update, context, q, transcript)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback: answer by text if there's an open card."""
    chat_id = update.effective_chat.id
    user = db.upsert_user(chat_id)
    qid = user["pending_qid"]
    if not qid:
        return
    q = db.get_question(qid)
    if not q or q["answered"]:
        return
    await grade_and_reply(update, context, q, update.message.text.strip())


# =========================================================================
# Startup
# =========================================================================
async def on_startup(app: Application):
    db.init_db()
    enabled = db.all_enabled_users()
    for u in enabled:
        if not u["level_started_at"]:
            db.set_field(u["chat_id"], "level_started_at", _utcnow_iso())
        schedule_next(app.job_queue, u["chat_id"])
        schedule_summary(app.job_queue, u["chat_id"])
    log.info("rescheduled %d enabled user(s)", len(enabled))


def main():
    db.init_db()

    # Python 3.12+ stopped auto-creating an event loop in the main thread, and
    # python-telegram-bot 21.x calls asyncio.get_event_loop() inside run_polling().
    # Create one up front so it works on Python 3.13 / 3.14.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(on_startup)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ask", cmd_ask))
    app.add_handler(CommandHandler("next", cmd_next))
    app.add_handler(CommandHandler("followup", cmd_followup))
    app.add_handler(CommandHandler("skipfollowup", cmd_skipfollowup))
    app.add_handler(CommandHandler("progress", cmd_progress))
    app.add_handler(CommandHandler("levels", cmd_levels))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("goal", cmd_goal))
    app.add_handler(CommandHandler("target", cmd_target))
    app.add_handler(CommandHandler("interview", cmd_interview))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("coach", cmd_coach))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("sethours", cmd_sethours))
    app.add_handler(CommandHandler("settz", cmd_settz))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Coach is running. Press Ctrl-C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
