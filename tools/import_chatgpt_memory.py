#!/usr/bin/env python3
"""Import ChatGPT export conversations into Future memory JSON.

Usage:
  python tools/import_chatgpt_memory.py --export-dir C:\\path\\to\\chatgpt-export

Optional:
  --input-file C:\\path\\to\\conversations.json
  --output-file C:\\path\\to\\future_memory.json
  --dry-run
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _extract_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [str(x).strip() for x in content if str(x).strip()]
        return "\n".join(parts).strip()
    if isinstance(content, dict):
        # ChatGPT exports typically store text under content.parts
        if isinstance(content.get("parts"), list):
            parts = [str(x).strip() for x in content["parts"] if str(x).strip()]
            return "\n".join(parts).strip()
        text = content.get("text")
        if isinstance(text, str):
            return text.strip()
    return str(content).strip()


def _message_role(message: dict[str, Any]) -> str:
    author = message.get("author") or {}
    if isinstance(author, dict):
        role = author.get("role")
        if isinstance(role, str):
            return role.strip().lower()
    return ""


def _extract_pairs_from_conversation(conversation: dict[str, Any]) -> list[dict[str, str]]:
    mapping = conversation.get("mapping")
    if not isinstance(mapping, dict):
        return []

    rows: list[tuple[float, str, str]] = []
    for node in mapping.values():
        if not isinstance(node, dict):
            continue
        message = node.get("message")
        if not isinstance(message, dict):
            continue

        role = _message_role(message)
        if role not in {"user", "assistant"}:
            continue

        content = message.get("content")
        text = _extract_text(content)
        if not text:
            continue

        created_at = message.get("create_time")
        ts = float(created_at) if isinstance(created_at, (int, float)) else 0.0
        rows.append((ts, role, text))

    rows.sort(key=lambda item: item[0])

    pairs: list[dict[str, str]] = []
    pending_user = ""

    for _, role, text in rows:
        if role == "user":
            pending_user = text
            continue
        if role == "assistant":
            pairs.append({"user": pending_user, "ai": text})
            pending_user = ""

    return pairs


def _extract_pairs(payload: Any) -> list[dict[str, str]]:
    if not isinstance(payload, list):
        return []

    pairs: list[dict[str, str]] = []
    for conversation in payload:
        if not isinstance(conversation, dict):
            continue
        pairs.extend(_extract_pairs_from_conversation(conversation))

    # De-duplicate while preserving order.
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in pairs:
        user_text = str(item.get("user", "")).strip()
        ai_text = str(item.get("ai", "")).strip()
        if not user_text and not ai_text:
            continue
        key = (user_text, ai_text)
        if key in seen:
            continue
        seen.add(key)
        out.append({"user": user_text, "ai": ai_text})

    return out


def _resolve_input_files(export_dir: Path | None, input_file: Path | None) -> list[Path]:
    if input_file:
        return [input_file]
    if not export_dir:
        raise ValueError("Provide either --input-file or --export-dir.")

    candidates = sorted(export_dir.glob("conversations*.json"))
    if candidates:
        return candidates

    raise FileNotFoundError(
        f"Could not find conversations*.json in {export_dir}. "
        "Please pass --input-file explicitly."
    )


def _load_existing(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        payload = _read_json(path)
    except Exception:
        return []
    if not isinstance(payload, list):
        return []

    out: list[dict[str, str]] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        user_text = str(entry.get("user", "")).strip()
        ai_text = str(entry.get("ai", "")).strip()
        if user_text or ai_text:
            out.append({"user": user_text, "ai": ai_text})
    return out


def _merge(existing: list[dict[str, str]], imported: list[dict[str, str]]) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for collection in (existing, imported):
        for item in collection:
            user_text = str(item.get("user", "")).strip()
            ai_text = str(item.get("ai", "")).strip()
            if not user_text and not ai_text:
                continue
            key = (user_text, ai_text)
            if key in seen:
                continue
            seen.add(key)
            merged.append({"user": user_text, "ai": ai_text})

    return merged


def main() -> int:
    parser = argparse.ArgumentParser(description="Import ChatGPT export into Future memory format.")
    parser.add_argument("--export-dir", type=Path, default=None, help="Path to ChatGPT export folder.")
    parser.add_argument("--input-file", type=Path, default=None, help="Path to conversations.json.")
    parser.add_argument(
        "--output-file",
        type=Path,
        default=Path("future_memory.json"),
        help="Destination Future memory file.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not write output; print counts only.")
    args = parser.parse_args()

    input_files = _resolve_input_files(args.export_dir, args.input_file)

    imported: list[dict[str, str]] = []
    for path in input_files:
        payload = _read_json(path)
        imported.extend(_extract_pairs(payload))

    # Final de-dup across all source files.
    imported = _merge([], imported)
    existing = _load_existing(args.output_file)
    merged = _merge(existing, imported)

    print(f"Input files: {len(input_files)}")
    if len(input_files) <= 10:
        for path in input_files:
            print(f" - {path}")
    else:
        print(f" - first: {input_files[0]}")
        print(f" - last:  {input_files[-1]}")
    print(f"Imported pairs: {len(imported)}")
    print(f"Existing pairs: {len(existing)}")
    print(f"Merged pairs: {len(merged)}")

    if args.dry_run:
        print("Dry run complete. No files were written.")
        return 0

    args.output_file.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    print(f"Wrote merged memory to: {args.output_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
