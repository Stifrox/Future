"""File-backed notes storage: create, resume, search, and save note tabs."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

_APP_ROOT = Path(__file__).resolve().parent.parent
NOTES_DIR = _APP_ROOT / "data" / "notes"
INDEX_PATH = _APP_ROOT / "data" / "notes_index.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slugify(title: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", (title or "").strip().lower()).strip("-")
    return cleaned or "note"


def _load_index() -> List[Dict]:
    if not INDEX_PATH.exists():
        return []
    try:
        payload = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        return payload.get("notes", []) if isinstance(payload, dict) else []
    except Exception:
        return []


def _save_index(notes: List[Dict]) -> None:
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps({"notes": notes}, indent=2), encoding="utf-8")


def _unique_id(base_id: str, existing_ids: set) -> str:
    if base_id not in existing_ids:
        return base_id
    counter = 2
    while f"{base_id}-{counter}" in existing_ids:
        counter += 1
    return f"{base_id}-{counter}"


def _preview(content: str, limit: int = 140) -> str:
    flat = re.sub(r"\s+", " ", (content or "").strip())
    return flat[:limit] + ("..." if len(flat) > limit else "")


def _remember_note(title: str, content: str) -> None:
    try:
        from tools.memory import load_memory, remember, save_memory
        memory = load_memory()
        remember(memory, f"Saved a note titled '{title}'", _preview(content, limit=300) or "(empty note)")
        save_memory(memory)
    except Exception:
        pass


def list_notes() -> List[Dict]:
    notes = _load_index()
    return sorted(notes, key=lambda n: n.get("updated", ""), reverse=True)


def _find_entry(note_id: str) -> Optional[Dict]:
    for entry in _load_index():
        if entry.get("id") == note_id:
            return entry
    return None


def search_notes(query: str, limit: int = 5) -> List[Dict]:
    q = (query or "").strip().lower()
    if not q:
        return []
    q_words = set(re.findall(r"[a-z0-9]+", q))

    scored = []
    for entry in _load_index():
        title = str(entry.get("title", "")).lower()
        preview = str(entry.get("preview", "")).lower()
        score = 0.0
        if q == title:
            score += 10
        if q in title:
            score += 5
        title_words = set(re.findall(r"[a-z0-9]+", title))
        score += 2 * len(q_words & title_words)
        if q in preview:
            score += 1
        if score > 0:
            scored.append((score, entry))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [entry for _, entry in scored[:limit]]


def get_note(note_id: str) -> Optional[Dict]:
    entry = _find_entry(note_id)
    if not entry:
        return None
    path = NOTES_DIR / entry["filename"]
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    return {**entry, "content": content}


def create_note(title: str, content: str = "") -> Dict:
    title = (title or "").strip() or "Untitled Note"
    notes = _load_index()
    existing_ids = {entry.get("id") for entry in notes}
    note_id = _unique_id(_slugify(title), existing_ids)
    filename = f"{note_id}.txt"

    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    (NOTES_DIR / filename).write_text(content or "", encoding="utf-8")

    now = _now_iso()
    entry = {
        "id": note_id,
        "title": title,
        "filename": filename,
        "created": now,
        "updated": now,
        "preview": _preview(content),
    }
    notes.append(entry)
    _save_index(notes)
    _remember_note(title, content)
    return {**entry, "content": content or ""}


def save_note(note_id: str, content: str, title: Optional[str] = None) -> Optional[Dict]:
    notes = _load_index()
    for entry in notes:
        if entry.get("id") == note_id:
            if title and title.strip():
                entry["title"] = title.strip()
            entry["updated"] = _now_iso()
            entry["preview"] = _preview(content)
            NOTES_DIR.mkdir(parents=True, exist_ok=True)
            (NOTES_DIR / entry["filename"]).write_text(content or "", encoding="utf-8")
            _save_index(notes)
            _remember_note(entry["title"], content)
            return {**entry, "content": content or ""}
    return None


def delete_note(note_id: str) -> bool:
    notes = _load_index()
    remaining = [entry for entry in notes if entry.get("id") != note_id]
    if len(remaining) == len(notes):
        return False
    removed = next((entry for entry in notes if entry.get("id") == note_id), None)
    if removed:
        path = NOTES_DIR / removed["filename"]
        if path.exists():
            path.unlink()
    _save_index(remaining)
    return True
