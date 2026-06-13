"""AI layer — OpenAI only.

Flashcard model:
- generate_question: one atomic question, answerable in a word / one short sentence.
- grade_answer: score plus targeted interview coaching feedback.
- transcribe: Whisper (Telegram voice notes are OGG/Opus, accepted directly).

JSON mode guarantees parseable structured output. Calls are blocking and run off
the event loop via asyncio.to_thread() in bot.py. Only model + messages are passed
so the code works across GPT model families.
"""
import json
import re

from openai import OpenAI

import config

_client = OpenAI(api_key=config.OPENAI_API_KEY)
_AGENT_ACTIONS = {
    "ask_flashcard",
    "review_weakness",
    "make_plan",
    "progress_review",
}


def _chat(system: str, user: str, json_mode: bool = True) -> str:
    kwargs = {
        "model": config.OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = _client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


def _parse_json(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.IGNORECASE).strip()
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object found in: {text[:200]!r}")
    return json.loads(t[start:end + 1])


# --- transcription --------------------------------------------------------
def transcribe(audio_path: str) -> str:
    with open(audio_path, "rb") as f:
        tr = _client.audio.transcriptions.create(model=config.WHISPER_MODEL, file=f)
    return (tr.text or "").strip()


# --- flashcard question generation ----------------------------------------
def generate_question(topic: str, difficulty: str, avoid: list[str]) -> str:
    avoid_block = "\n".join(f"- {q}" for q in avoid[:20]) or "(none yet)"
    system = (
        "You write quick flashcard-style interview questions for ML/AI roles. "
        "One atomic concept per card — like the front of a flashcard."
    )
    user = f"""Create ONE flashcard question on the topic: "{topic}".
Difficulty: {difficulty}.

STRICT rules:
- Test exactly ONE concept. NO multi-part questions, no "and", no "compare X and Y",
  no "what is the impact of X on Y". Just one simple thing.
- It must be answerable in a single word or one short sentence.
- One line only, crisp, like a flashcard front.
- Prefer "What is...", "Which...", "Name the...", "What does X do?", "True or false:...".
- Do NOT repeat or closely resemble any of these recent questions:
{avoid_block}

Respond with ONLY this JSON: {{"question": "<the question>"}}"""
    return _parse_json(_chat(system, user))["question"].strip()


_MISTAKE_TYPES = {
    "none",
    "missing_keyword",
    "vague_explanation",
    "confused_concept",
    "wrong_formula",
    "wrong_example",
    "incomplete_answer",
    "off_topic",
    "empty_answer",
}


def _clean_str(data: dict, key: str) -> str:
    return str(data.get(key) or "").strip()


def _clean_list(data: dict, key: str, limit: int = 5) -> list[str]:
    raw = data.get(key) or []
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw[:limit] if str(item).strip()]


# --- grading (concise, interview-focused) ---------------------------------
def grade_answer(question: str, topic: str, transcript: str) -> dict:
    system = (
        "You grade a flashcard answer for ML/AI interview prep. The answer was "
        "spoken aloud and auto-transcribed, so ignore filler words and minor "
        "transcription errors. Judge whether the core concept is right. Be accurate, "
        "specific, and practical for interviews. Keep each field concise."
    )
    user = f"""Flashcard topic: {topic}
Question: {question}

Candidate's spoken answer:
\"\"\"{transcript}\"\"\"

Score 0-10 (10 = fully correct and precise; ~5 = partially right; 0 = wrong/empty).
Pick exactly one mistake_type:
- none: correct or nearly correct
- missing_keyword: mostly right but missed a key term
- vague_explanation: too hand-wavy for an interview
- confused_concept: mixed up related ideas
- wrong_formula: formula/math is wrong or missing
- wrong_example: example is misleading or incorrect
- incomplete_answer: important part missing
- off_topic: did not answer the question
- empty_answer: no meaningful answer

Respond with ONLY this JSON:
{{
  "correct": <true|false>,
  "score": <number 0-10>,
  "answer": "<the correct answer as a single word or one short sentence>",
  "note": "<at most one short sentence of clarification, or an empty string>",
  "mistake_type": "<one mistake_type>",
  "interview_answer": "<a polished 1-2 sentence answer the candidate could say aloud>",
  "follow_up": "<one natural interviewer follow-up question>",
  "trap": "<one common misconception or trap, or an empty string>"
}}"""
    data = _parse_json(_chat(system, user))
    data["score"] = max(0.0, min(10.0, float(data.get("score", 0))))
    data["correct"] = bool(data.get("correct", data["score"] >= 6))
    data["answer"] = _clean_str(data, "answer")
    data["note"] = _clean_str(data, "note")
    data["interview_answer"] = _clean_str(data, "interview_answer")
    data["follow_up"] = _clean_str(data, "follow_up")
    data["trap"] = _clean_str(data, "trap")
    mistake_type = _clean_str(data, "mistake_type").lower()
    data["mistake_type"] = mistake_type if mistake_type in _MISTAKE_TYPES else "none"
    if data["score"] < 8 and data["mistake_type"] == "none":
        data["mistake_type"] = "incomplete_answer"
    return data


# --- study tip ------------------------------------------------------------
def study_tip(weak_topics: list[str]) -> str:
    if not weak_topics:
        return ""
    topics = ", ".join(weak_topics)
    system = "You are a supportive ML/AI interview coach."
    user = (
        f"A candidate preparing for ML/AI interviews is weakest in: {topics}. "
        "Give a short, motivating study suggestion (2-3 sentences) on what to "
        "focus on next and how. No preamble, no lists."
    )
    return _chat(system, user, json_mode=False).strip()


# --- agent planning -------------------------------------------------------
def build_study_plan(state: dict) -> dict:
    system = (
        "You are an autonomous ML/AI interview prep agent. Build a practical, "
        "short study plan from the user's goal, progress, weak topics, and answer "
        "patterns. Keep it specific and action-oriented."
    )
    user = f"""User state JSON:
{json.dumps(state, indent=2)}

Respond with ONLY this JSON:
{{
  "summary": "<one sentence about the prep strategy>",
  "priorities": ["<priority 1>", "<priority 2>", "<priority 3>"],
  "today": ["<task 1>", "<task 2>", "<task 3>"],
  "this_week": ["<task 1>", "<task 2>", "<task 3>"],
  "success_metric": "<how the user should know the plan is working>"
}}"""
    data = _parse_json(_chat(system, user))
    return {
        "summary": _clean_str(data, "summary"),
        "priorities": _clean_list(data, "priorities", limit=5),
        "today": _clean_list(data, "today", limit=5),
        "this_week": _clean_list(data, "this_week", limit=5),
        "success_metric": _clean_str(data, "success_metric"),
    }


def decide_next_action(state: dict) -> dict:
    system = (
        "You are the decision-making layer for an ML/AI interview prep agent. "
        "Choose the single next best coaching action from the allowed actions. "
        "Do not invent actions. Prefer direct practice when the user has a plan "
        "and no unanswered card is pending."
    )
    user = f"""Allowed actions:
- ask_flashcard: ask one targeted question now
- review_weakness: review recurring weak topics or mistake patterns
- make_plan: create or refresh the study plan
- progress_review: summarize progress and next focus

User state JSON:
{json.dumps(state, indent=2)}

Respond with ONLY this JSON:
{{
  "action": "<one allowed action>",
  "reason": "<one short sentence>",
  "message": "<short text to send before taking the action>",
  "focus_topic": "<topic to emphasize, or empty string>"
}}"""
    data = _parse_json(_chat(system, user))
    action = _clean_str(data, "action")
    if action not in _AGENT_ACTIONS:
        action = "make_plan" if not state.get("prep_goal") else "ask_flashcard"
    return {
        "action": action,
        "reason": _clean_str(data, "reason"),
        "message": _clean_str(data, "message"),
        "focus_topic": _clean_str(data, "focus_topic"),
    }
