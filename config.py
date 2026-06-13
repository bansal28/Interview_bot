"""Configuration and constants for the ML/AI interview coach bot.

Flashcard model: short, atomic questions (one-word / one-sentence answers),
organized into difficulty LEVELS. Each level has a question count and a target
number of days. Single provider: OpenAI (Whisper + a GPT model).
"""
import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

# --- Required secrets -----------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

# --- Models (OpenAI) ------------------------------------------------------
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "whisper-1")

# --- Per-user defaults (editable at runtime) ------------------------------
DEFAULT_TZ = os.environ.get("DEFAULT_TZ", "Europe/London")
DEFAULT_ACTIVE_START = int(os.environ.get("DEFAULT_ACTIVE_START", "9"))   # 09:00
DEFAULT_ACTIVE_END = int(os.environ.get("DEFAULT_ACTIVE_END", "21"))      # 21:00
DEFAULT_SUMMARY_HOUR = int(os.environ.get("DEFAULT_SUMMARY_HOUR", "21"))  # recap hour

DB_PATH = os.environ.get("DB_PATH", "coach.db")

# --- Levels ---------------------------------------------------------------
# Difficulty rises level by level. `questions` = how many to clear the level,
# `days` = your target window for it. `difficulty` is fed to the question
# generator. Edit freely: add levels, change counts/days, retune difficulty.
LEVELS = [
    {"name": "Fundamentals",     "difficulty": "very easy recall (definitions, one keyword answers)", "questions": 15, "days": 2},
    {"name": "Core Concepts",    "difficulty": "easy (single concept, short factual answer)",          "questions": 12, "days": 3},
    {"name": "Applied Basics",   "difficulty": "medium (apply one concept, one-sentence answer)",      "questions": 12, "days": 3},
    {"name": "Advanced",         "difficulty": "hard (deeper single-concept reasoning, still concise)", "questions": 10, "days": 4},
    {"name": "Interview-Grade",  "difficulty": "expert (tricky one-liner an interviewer would ask)",    "questions": 10, "days": 4},
]
LEVEL_COUNT = len(LEVELS)

# Pace (questions/day) once all levels are cleared and you're in review mode.
REVIEW_PACE = int(os.environ.get("REVIEW_PACE", "8"))

# --- Topic bank -----------------------------------------------------------
# Topics span all levels; the same topic gets easier/harder by level. Weak
# topics are asked more often. Edit freely.
TOPICS = [
    "Classical ML: bias-variance, overfitting, regularization",
    "Linear & logistic regression",
    "Decision trees, random forests, gradient boosting",
    "SVMs and kernels",
    "Clustering & dimensionality reduction (k-means, PCA)",
    "Probability & statistics for ML",
    "Evaluation metrics (precision/recall, ROC-AUC, F1)",
    "Feature engineering & data leakage",
    "Neural network fundamentals & backpropagation",
    "Optimizers & learning-rate schedules",
    "Regularization in deep learning (dropout, batch/layer norm)",
    "CNNs & computer vision",
    "RNNs, LSTMs & sequence modeling",
    "Transformers & attention",
    "LLMs: pretraining, fine-tuning, RLHF",
    "Tokenization & embeddings",
    "Retrieval-augmented generation (RAG)",
    "Prompting & evaluation of LLMs",
    "ML system design",
    "MLOps: deployment, monitoring, drift",
]


def tz_of(name: str) -> ZoneInfo:
    """Safe timezone lookup that falls back to the default."""
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


def level_def(n: int):
    """Return the level dict for level n (1-based), or None if past the last level."""
    if 1 <= n <= LEVEL_COUNT:
        return LEVELS[n - 1]
    return None