# Interview Bot: ML/AI Interview Coach Agent

A Telegram-based AI interview prep coach for ML/AI roles. It sends short
flashcard-style questions during your active hours, accepts voice or text answers,
grades them with OpenAI, tracks your weak areas, and uses an agent layer to plan
what you should practise next.

The core interaction is intentionally lightweight: Telegram is for daily practice
and reminders; the SQLite database keeps your progress, profile, plan, questions,
answers, and coaching history.

## What It Does

- Sends ML/AI interview questions automatically during your configured active hours.
- Lets you answer with Telegram voice notes or plain text.
- Uses Whisper for voice transcription.
- Uses an OpenAI chat model for question generation, grading, coaching feedback,
  study plans, and next-action decisions.
- Tracks level progress, topic scores, recurring mistake patterns, and recent answers.
- Stores a prep goal, target role, interview date, personalized plan, and agent actions.
- Makes follow-up questions optional via `/followup` instead of forcing every follow-up.

## How The Coaching Loop Works

```text
/start or scheduled tick
        |
        v
agent/user state from SQLite
        |
        v
choose next action
        |
        +--> ask a flashcard
        +--> review weak areas
        +--> refresh the study plan
        +--> summarize progress
        |
        v
user answers by voice or text
        |
        v
Whisper transcribes voice answers
        |
        v
OpenAI grades the answer
        |
        v
SQLite stores score, topic, mistake type, feedback, and optional follow-up
```

## Automatic Questions

Automatic questions only arrive while the bot process is running. If the terminal,
server, or machine stops, Telegram will not receive scheduled cards.

The scheduler:

- sends cards only inside your active hours
- spaces cards across the day based on your current level pace
- avoids stacking questions when one card is already pending
- sends reminders for unanswered pending cards
- does not count manual `/ask` questions against the automatic daily quota

Useful scheduling commands:

- `/next` shows when the next automatic question is due.
- `/settings` shows your active hours, timezone, daily recap time, goal, and next card.
- `/pause` stops automatic cards.
- `/resume` restarts automatic cards and rebuilds the schedule.

If you keep using `/ask`, those are extra manual cards. They no longer block the
automatic drip.

## Optional Follow-Ups

After grading, the bot may generate a follow-up question. It is now optional.

- Use `/followup` if you want to attempt the saved follow-up.
- Use `/skipfollowup` to clear it.
- If you ignore it, your normal automatic schedule continues.

Only one optional follow-up is stored at a time. A new graded answer can replace
the previous saved follow-up.

## Levels

| Level | Name | Cards | Target Days | Difficulty |
|---|---|---:|---:|---|
| 1 | Fundamentals | 15 | 2 | very easy recall |
| 2 | Core Concepts | 12 | 3 | easy |
| 3 | Applied Basics | 12 | 3 | medium |
| 4 | Advanced | 10 | 4 | hard |
| 5 | Interview-Grade | 10 | 4 | expert |

Edit `LEVELS` in `config.py` to change counts, days, names, or difficulty.

After all levels are cleared, the bot switches into review mode and focuses on
weaker topics.

## Prerequisites

- Python 3.11 to 3.14
- Telegram bot token from `@BotFather`
- OpenAI API key

## Setup

```bash
cd "/Users/hritikbansal/Desktop/ai projects/interview_helper"
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set:

```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
OPENAI_API_KEY=your_openai_api_key
```

Optional `.env` settings:

```env
OPENAI_MODEL=gpt-5-mini
WHISPER_MODEL=whisper-1
DEFAULT_TZ=Europe/London
DEFAULT_ACTIVE_START=9
DEFAULT_ACTIVE_END=21
DEFAULT_SUMMARY_HOUR=21
REVIEW_PACE=8
DB_PATH=coach.db
```

## Run

Use the project virtual environment:

```bash
.venv/bin/python bot.py
```

Then open Telegram and send:

```text
/start
```

The bot will register you, schedule automatic cards, and send the first card
immediately if there is no pending question.

## Recommended First-Time Flow

```text
/start
/goal Prepare me for an ML Engineer interview in 30 days
/target ML Engineer at a product company
/interview 2026-07-15
/plan
/coach
```

## Commands

| Command | What it does |
|---|---|
| `/start` | Register, resume scheduling, and send the first card if possible |
| `/ask` | Ask an extra manual card immediately |
| `/next` | Show the next scheduled automatic card time |
| `/followup` | Attempt the optional follow-up from your last graded answer |
| `/skipfollowup` | Clear the saved optional follow-up |
| `/progress` | Show current level, progress bar, deadline, and pace |
| `/levels` | Show the full level map |
| `/stats` | Show strong topics, weak topics, and common answer issues |
| `/goal <goal>` | Set the agent's prep objective |
| `/target <role>` | Set target role or interview type |
| `/interview <date>` | Store interview date, ideally `YYYY-MM-DD` |
| `/profile` | Show the agent's current user model |
| `/plan` | Generate or refresh a personalized study plan |
| `/coach` | Let the agent choose the next best coaching action |
| `/pause` | Stop automatic cards |
| `/resume` | Restart automatic cards |
| `/settings` | Show schedule, timezone, current goal, and next card |
| `/sethours 9 21` | Set active hours in 24-hour time |
| `/settz Europe/London` | Set timezone with an IANA timezone name |

## Files

| File | Purpose |
|---|---|
| `bot.py` | Telegram handlers, scheduling, grading flow, agent execution |
| `ai.py` | OpenAI calls for generation, grading, planning, and agent decisions |
| `db.py` | SQLite schema, migrations, persistence, stats |
| `config.py` | Defaults, levels, topics, model names |
| `test_logic.py` | Unit tests for scheduling, DB behavior, and logic helpers |
| `coach.db` | Local SQLite database, ignored by git |

## Data And Persistence

The bot stores data in SQLite:

- users and settings
- current level and pending question
- questions and answers
- scores and mistake types
- optional follow-up question
- prep goal, target role, interview date
- agent plan and agent action log

The schema migrates automatically on startup via `db.init_db()`.

Back up `coach.db` if you want to preserve history.

## Testing

```bash
.venv/bin/python -m unittest -v
.venv/bin/python -m py_compile ai.py db.py bot.py test_logic.py
```

## Keeping It Running

For automatic cards, the bot must stay online. On a server, run it under systemd:

```ini
[Unit]
Description=ML Interview Coach Agent
After=network-online.target

[Service]
WorkingDirectory=/home/youruser/interview_helper
ExecStart=/home/youruser/interview_helper/.venv/bin/python bot.py
Restart=always
EnvironmentFile=/home/youruser/interview_helper/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now coach
journalctl -u coach -f
```

Free hosts that sleep idle apps are a poor fit because scheduled questions will
not fire while the process is asleep.

## Notes

- Only one main card is pending at a time.
- Manual `/ask` cards are extra and do not consume the automatic daily drip quota.
- Optional follow-ups are separate from the main card queue.
- The bot is an interview prep assistant, not an authority on correctness. Review
  important answers against trusted ML/AI references when needed.
