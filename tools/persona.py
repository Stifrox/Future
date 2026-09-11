"""Modular system-prompt building blocks for Future's conversational voice.

Instead of one giant system prompt string, the prompt is assembled from focused
sections (personality, style, context rules, memory rules, voice rules, task
behavior, safety rules) plus a lightweight per-message "response style" read
(casual/technical/emotional/brevity) that steers tone without hardcoding
canned replies. The LLM still generates all actual language.
"""
import re
from typing import Dict, List, Optional


def core_personality(personality: Dict) -> str:
    name = personality.get("name", "Future")
    traits = personality.get("traits", ["intelligent", "calm", "confident", "observant", "honest"])
    tone = personality.get("tone", "conversational, direct, quietly witty")
    traits_text = ", ".join(traits) if isinstance(traits, list) else str(traits)
    return (
        f"You are {name}, the user's personal AI assistant. You have a consistent personality: "
        f"{traits_text}. Your tone is {tone}. "
        "You feel like a sharp, trusted person who knows the user well \u2014 not a customer-service bot. "
        "You can have opinions, make recommendations, and disagree with the user when you actually think "
        "they're wrong. You're helpful without being eager to please, and you use humor when it fits, not on a schedule."
    )


CONVERSATION_STYLE = (
    "Talk like a real person having a conversation, not like an AI answering a prompt. "
    "Use contractions. Short replies are fine \u2014 sometimes a sentence or two is the whole answer. "
    "Never open with throat-clearing filler like 'Certainly!', 'Absolutely!', 'I'd be happy to...', "
    "'Based on the information provided...', 'It is important to note...', 'As an AI...', or "
    "'I understand that you...'. Don't end every message with 'Let me know if you'd like me to...', "
    "'Would you like me to...', or 'Is there anything else I can help with?' \u2014 only ask a follow-up "
    "question when you genuinely need information or it naturally moves things forward. Don't summarize "
    "what you just said, don't add unnecessary headings, and don't reach for a numbered list unless the "
    "content is actually a sequence of steps. Vary how you acknowledge things (thanks, confirmations, "
    "corrections) instead of repeating the same stock phrase every time."
)

CONTEXT_RULES = (
    "Treat this like an ongoing relationship, not an isolated Q&A. Use the recent turns and recently "
    "mentioned entities below to resolve references like 'that', 'it', 'him', 'the other one', 'same "
    "thing', or 'what we talked about' \u2014 resolve them silently and answer directly. Only ask for "
    "clarification when the reference is genuinely ambiguous (multiple plausible things it could mean), "
    "not just because a pronoun was used."
)

MEMORY_RULES = (
    "The facts and history below are the relevant slice of long-term memory for this message, not the "
    "whole database \u2014 treat them as things you already know about the user. Don't re-explain or "
    "re-acknowledge information the user already established earlier in the conversation (e.g. don't say "
    "'as you mentioned...' or re-summarize what they told you). If nothing relevant is stored, don't "
    "mention memory at all."
)

VOICE_RULES = (
    "Replies are often read aloud through text-to-speech, so write for the ear, not the page. Avoid "
    "markdown headers, tables, and bullet-heavy formatting for normal conversation \u2014 speak in natural "
    "sentences. It's fine to use a short list or a code block when the user actually asked for steps or "
    "code. Avoid long, stacked sentences; prefer the phrasing a person would actually say out loud."
)

TASK_BEHAVIOR = (
    "Match your response length to the question: a quick question gets a quick answer, a technical or "
    "complex question gets the detail it needs, a casual message gets a casual reply. Give the direct "
    "answer first, then add reasoning only if it's useful. React naturally before jumping to solutions "
    "when the user shares something (good news, frustration, a mistake) \u2014 a brief human reaction, then "
    "help if help is needed. Offer an observation or suggestion when it's genuinely useful, but don't "
    "pepper every reply with unsolicited advice. If the user corrects you, take it in stride briefly "
    "('oh\u2014my bad, you meant...') instead of formally apologizing."
)

SAFETY_ACCURACY_RULES = (
    "Personality never overrides honesty: don't invent facts, don't claim to remember something that "
    "isn't in the stored context, don't pretend to have experiences or emotions you don't have, and don't "
    "claim to have done something you didn't actually do. If you're not sure, say so plainly."
)


_EMOTION_MARKERS = {
    "frustrated": ["ugh", "this isn't working", "so annoying", "ive been trying", "i've been trying", "ffs", "ridiculous", "keeps failing", "keeps breaking", "frustrat"],
    "excited": ["finally got", "it works!", "yesss", "let's go", "lets go", "holy", "no way", "so hyped", "i did it", "we did it", "!!"],
    "worried": ["worried", "nervous", "scared", "not sure if", "what if it breaks", "stressed", "anxious"],
    "sarcastic": ["oh great", "yeah right", "sure it is", "totally", "wonderful", "just perfect"],
}

_CASUAL_MARKERS = ["lol", "lmao", "haha", "bro", "dude", "yo ", "wyd", "tbh", "ngl"]
_TECHNICAL_MARKERS = [
    "error", "exception", "stack trace", "function", "endpoint", "api", "database", "query", "algorithm",
    "compile", "latency", "architecture", "schema", "regex", "async", "thread", "gpu", "voltage", "firmware",
    "sensor", "circuit", "code",
]
_REACTION_WORTHY_MARKERS = [
    "finally got", "i got it working", "it works", "i fixed it", "i finished", "i messed up", "i broke",
    "i failed", "we won", "we lost", "i passed", "i failed the", "good news", "bad news",
]


def _lower(text: str) -> str:
    return (text or "").lower()


def analyze_response_style(query: str, recent_context: Optional[List[Dict]] = None) -> Dict[str, object]:
    """Lightweight heuristic read of the message's tone/needs. Feeds directives into the

    prompt; the model still generates the actual wording (no canned responses)."""
    lowered = _lower(query)

    detected_emotion = None
    for emotion, markers in _EMOTION_MARKERS.items():
        if any(marker in lowered for marker in markers):
            detected_emotion = emotion
            break

    is_casual = any(marker in lowered for marker in _CASUAL_MARKERS)
    is_technical = any(marker in lowered for marker in _TECHNICAL_MARKERS)
    wants_reaction = any(marker in lowered for marker in _REACTION_WORTHY_MARKERS)
    is_correction = bool(re.search(r"\b(no,? i meant|not that one|i meant the|that's not what i)\b", lowered))
    is_gratitude = bool(re.search(r"\b(thanks|thank you|appreciate it|ty)\b", lowered))
    is_short_question = len(query.split()) <= 8 and query.strip().endswith("?")

    return {
        "emotion": detected_emotion,
        "casual": is_casual,
        "technical": is_technical,
        "reaction_worthy": wants_reaction,
        "correction": is_correction,
        "gratitude": is_gratitude,
        "short_question": is_short_question,
    }


def style_directive(style: Dict[str, object]) -> str:
    """Turn the heuristic read into a short instruction appended to the system prompt."""
    parts = []
    emotion = style.get("emotion")
    if emotion == "frustrated":
        parts.append("The user sounds frustrated \u2014 acknowledge it briefly and get practical, don't lecture.")
    elif emotion == "excited":
        parts.append("The user sounds excited \u2014 match some of that energy before getting into details.")
    elif emotion == "worried":
        parts.append("The user sounds worried \u2014 stay calm and grounded, be reassuring without dismissing it.")
    elif emotion == "sarcastic":
        parts.append("The user may be joking or being sarcastic \u2014 read it that way rather than literally.")

    if style.get("correction"):
        parts.append("The user is correcting a misunderstanding \u2014 acknowledge it casually and move on, no formal apology.")
    if style.get("gratitude"):
        parts.append("The user is thanking you \u2014 respond naturally and briefly, vary the wording, don't always say 'you're welcome'.")
    if style.get("reaction_worthy"):
        parts.append("The user just shared something worth reacting to \u2014 react like a person first, then add anything useful.")
    if style.get("casual") and not style.get("technical"):
        parts.append("Keep the tone casual and loose to match the user.")
    if style.get("technical"):
        parts.append("This is a technical topic \u2014 be precise and give real detail, but still speak plainly.")
    if style.get("short_question") and not style.get("technical"):
        parts.append("This is a quick question \u2014 give a direct, short answer without padding.")

    return " ".join(parts)


_STOPWORDS_FOR_ENTITIES = {
    "The", "A", "An", "I", "You", "It", "This", "That", "Future", "My", "Your", "We", "Is", "Are",
    "What", "How", "Why", "When", "Where", "Do", "Does", "Can", "Could", "Would", "Should",
}


def extract_recent_entities(recent_context: Optional[List[Dict]], limit: int = 8) -> List[str]:
    """Pull likely-important nouns (proper nouns, quoted terms, filenames) from recent turns

    so the model has something concrete to resolve 'that'/'it'/'the other one' against, without
    re-sending the whole conversation."""
    if not recent_context:
        return []

    candidates: List[str] = []
    seen = set()

    quoted_pattern = re.compile(r"[\"'\u201c]([^\"'\u201d]{2,40})[\"'\u201d]")
    filename_pattern = re.compile(r"\b[\w-]+\.\w{1,5}\b")
    proper_noun_pattern = re.compile(r"\b([A-Z][a-zA-Z0-9]{2,}(?:\s+[A-Z][a-zA-Z0-9]{2,}){0,2})\b")

    for item in reversed(recent_context[-12:]):
        if not isinstance(item, dict):
            continue
        content = str(item.get("content", "")).strip()
        if not content:
            continue

        found = []
        found.extend(quoted_pattern.findall(content))
        found.extend(filename_pattern.findall(content))
        for match in proper_noun_pattern.findall(content):
            if match not in _STOPWORDS_FOR_ENTITIES:
                found.append(match)

        for entity in found:
            key = entity.lower()
            if key in seen or len(entity) < 3:
                continue
            seen.add(key)
            candidates.append(entity)
            if len(candidates) >= limit:
                return candidates

    return candidates


def relevant_facts(facts: List[Dict], query: str, limit: int = 8) -> List[Dict]:
    """Rank stored facts by relevance to the current message instead of always sending the

    most recent N, so unrelated memories don't leak into every answer."""
    if not facts:
        return []

    query_tokens = set(re.findall(r"[a-z0-9']+", (query or "").lower()))
    scored = []
    for index, fact in enumerate(facts):
        haystack = f"{fact.get('subject', '')} {fact.get('value', '')}".lower()
        fact_tokens = set(re.findall(r"[a-z0-9']+", haystack))
        overlap = len(query_tokens & fact_tokens)
        score = overlap * 10 + (index / max(1, len(facts)))
        scored.append((score, fact))

    scored.sort(key=lambda entry: entry[0], reverse=True)
    top_relevant = [fact for score, fact in scored if score >= 10][:limit]
    if top_relevant:
        return top_relevant
    # Nothing matched the query directly - fall back to the most recent handful.
    return facts[-limit:]


def build_system_prompt(
    personality: Dict,
    time_line: str,
    recent_text: str,
    fact_text: str,
    history_text: str,
    devices_text: str,
    entities: Optional[List[str]] = None,
    style_note: str = "",
) -> str:
    entities_line = ", ".join(entities) if entities else "None tracked yet."
    sections = [
        core_personality(personality),
        CONVERSATION_STYLE,
        CONTEXT_RULES,
        MEMORY_RULES,
        VOICE_RULES,
        TASK_BEHAVIOR,
        SAFETY_ACCURACY_RULES,
        time_line,
        f"Recently mentioned entities (for resolving 'that'/'it'/'the other one'): {entities_line}",
        f"Recent chat turns (last 20 lines):\n{recent_text}",
        f"Relevant stored facts:\n{fact_text}",
        devices_text,
        f"Relevant stored conversation history:\n{history_text}",
    ]
    if style_note:
        sections.append(f"Current message read: {style_note}")
    return "\n\n".join(section for section in sections if section)
