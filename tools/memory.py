import json
import re
from pathlib import Path

import config


_TOKEN_RE = re.compile(r"[a-z0-9']+")
_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "at",
    "can",
    "could",
    "do",
    "for",
    "i",
    "is",
    "it",
    "me",
    "my",
    "of",
    "please",
    "remember",
    "that",
    "the",
    "to",
    "what",
    "you",
}


def _tokenize(text):
    return {
        token
        for token in _TOKEN_RE.findall((text or "").lower())
        if len(token) > 1 and token not in _STOPWORDS
    }


def _clean_fact_text(text):
    return re.sub(r"\s+", " ", (text or "").strip(" .!?\t\n\r")).strip()


def _memory_paths():
    root = Path(__file__).resolve().parent.parent
    primary = root / str(config.MEMORY_FILE)
    return [
        root / "data" / "future_memory.json",
        root / "data" / "long_term_memory.json",
        primary,
    ]


def _load_json_list(path):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception:
        return []
    return payload if isinstance(payload, list) else []


def _normalize_memory_entries(entries):
    normalized = []
    pending_user = None

    for entry in entries:
        if not isinstance(entry, dict):
            continue

        if "user" in entry or "ai" in entry:
            user_text = str(entry.get("user", "")).strip()
            ai_text = str(entry.get("ai", "")).strip()
            if user_text or ai_text:
                normalized.append({"user": user_text, "ai": ai_text})
            pending_user = None
            continue

        speaker = str(entry.get("speaker", "")).strip().lower()
        text = str(entry.get("text", "")).strip()
        if not speaker and not text:
            continue

        if speaker == "user":
            pending_user = text
            continue

        if speaker == "future":
            normalized.append({"user": pending_user or "", "ai": text})
            pending_user = None

    return normalized


def _dedupe_memory(entries):
    unique_entries = []
    seen = set()

    for entry in entries:
        user_text = str(entry.get("user", "")).strip()
        ai_text = str(entry.get("ai", "")).strip()
        key = (user_text, ai_text)
        if key in seen or (not user_text and not ai_text):
            continue
        seen.add(key)
        unique_entries.append({"user": user_text, "ai": ai_text})

    return unique_entries

def load_memory():
    merged_entries = []
    for path in _memory_paths():
        merged_entries.extend(_normalize_memory_entries(_load_json_list(path)))
    return _dedupe_memory(merged_entries)

def save_memory(memory):
    normalized = _dedupe_memory(memory)
    primary_path = _memory_paths()[-1]
    primary_path.write_text(json.dumps(normalized, indent=2), encoding="utf-8")

def remember(memory, user_input, ai_response):
    memory.append({"user": user_input, "ai": ai_response})
    if len(memory) > 500:
        memory.pop(0)  # Keep size manageable


def search_memory(memory, query, limit=5):
    query_text = (query or "").strip().lower()
    query_tokens = _tokenize(query_text)
    ranked = []

    for index, item in enumerate(memory):
        user_text = str(item.get("user", "")).strip()
        ai_text = str(item.get("ai", "")).strip()
        combined = f"{user_text}\n{ai_text}".strip()
        if not combined:
            continue

        combined_lower = combined.lower()
        item_tokens = _tokenize(combined_lower)
        overlap = len(query_tokens & item_tokens)
        score = overlap * 10

        if query_text and query_text in combined_lower:
            score += 25
        if "called" in query_text and "called" in combined_lower:
            score += 8
        if "project" in query_text and "project" in combined_lower:
            score += 6
        if "code" in query_text and "code" in combined_lower:
            score += 6
        if overlap == 0 and score == 0:
            continue

        score += index / max(1, len(memory))
        ranked.append((score, item))

    ranked.sort(key=lambda entry: entry[0], reverse=True)
    return [item for _, item in ranked[:limit]]


def extract_facts(memory):
    facts = []
    seen = set()
    patterns = [
        re.compile(r"\bmy (?P<subject>.+?) is called (?P<value>.+)$", re.IGNORECASE),
        re.compile(r"\bremember(?: that)? (?P<subject>.+?) is (?P<value>.+)$", re.IGNORECASE),
        re.compile(r"\b(?P<subject>.+? code(?: at .+?)?) is (?P<value>\d{3,})$", re.IGNORECASE),
    ]

    for item in memory:
        user_text = _clean_fact_text(str(item.get("user", "")))
        if not user_text:
            continue

        for pattern in patterns:
            match = pattern.search(user_text)
            if not match:
                continue

            subject = _clean_fact_text(match.group("subject"))
            value = _clean_fact_text(match.group("value"))
            if not subject or not value:
                continue

            key = (subject.lower(), value.lower())
            if key in seen:
                continue

            seen.add(key)
            facts.append({"subject": subject, "value": value, "source": user_text})

    return facts


def recall_fact(memory, query):
    query_text = _clean_fact_text(query)
    query_lower = query_text.lower()
    query_tokens = _tokenize(query_text)
    best_fact = None
    best_score = 0

    for fact in extract_facts(memory):
        subject = fact["subject"]
        value = fact["value"]
        subject_lower = subject.lower()
        value_lower = value.lower()
        fact_tokens = _tokenize(f"{subject} {value} {fact['source']}")
        overlap = len(query_tokens & fact_tokens)
        score = overlap * 10

        if subject_lower and subject_lower in query_lower:
            score += 20
        if value_lower and value_lower in query_lower:
            score += 8
        if "called" in query_lower and "called" in fact["source"].lower():
            score += 8
        if "code" in query_lower and "code" in subject_lower:
            score += 8

        if score > best_score:
            best_fact = fact
            best_score = score

    return best_fact if best_score > 0 else None
