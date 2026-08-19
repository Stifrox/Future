import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

LOG_PATH = Path("logs/update_requests.jsonl")
LAST_PLAN_PATH = Path("logs/last_update_plan.json")
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _choose_scope(instruction: str, target_files: List[str], scope: str) -> str:
    requested = (scope or "auto").strip().lower()
    if requested in {"small_edit", "full_rewrite"}:
        return requested

    lowered = (instruction or "").lower()
    small_markers = [
        "small edit",
        "small code",
        "minor edit",
        "quick edit",
        "one line",
        "one-line",
    ]
    if any(marker in lowered for marker in small_markers):
        return "small_edit"

    rewrite_markers = [
        "full rewrite",
        "rewrite",
        "re-architecture",
        "rearchitecture",
        "major refactor",
        "replace entire",
        "from scratch",
        "overhaul",
    ]
    if any(marker in lowered for marker in rewrite_markers):
        return "full_rewrite"
    return "small_edit"


def _choose_model(scope: str) -> str:
    if scope == "full_rewrite":
        return _env("ANTHROPIC_MODEL_FULL_REWRITE", "claude-opus-4-1")
    return _env("ANTHROPIC_MODEL_SMALL_EDIT", "claude-sonnet-4-5")


def _collect_file_context(target_files: List[str], max_chars_each: int = 8000) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for file_path in target_files:
        normalized = (file_path or "").strip()
        if not normalized:
            continue
        path = Path(normalized)
        if not path.exists() or not path.is_file():
            rows.append({"path": normalized, "status": "missing", "content": ""})
            continue

        content = path.read_text(encoding="utf-8", errors="replace")
        excerpt = content[:max_chars_each]
        rows.append(
            {
                "path": str(path),
                "status": "loaded",
                "content": excerpt,
            }
        )
    return rows


def _extract_json_object(text: str) -> Dict[str, object]:
    candidate = (text or "").strip()
    if not candidate:
        return {}

    # Accept direct JSON first.
    try:
        payload = json.loads(candidate)
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass

    # Fallback: extract first JSON object block from model text.
    match = re.search(r"\{[\s\S]*\}", candidate)
    if not match:
        return {}
    try:
        payload = json.loads(match.group(0))
        if isinstance(payload, dict):
            return payload
    except Exception:
        return {}
    return {}


def _call_anthropic(model: str, prompt: str, max_tokens: int = 2200) -> Dict[str, object]:
    api_key = _env("ANTHROPIC_API_KEY")
    if not api_key:
        return {"ok": False, "error": "ANTHROPIC_API_KEY is not configured."}

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    response = requests.post(
        ANTHROPIC_MESSAGES_URL,
        headers=headers,
        json={
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=90,
    )

    if not response.ok:
        # Fallback when a configured model is unavailable to this account/workspace.
        if response.status_code == 404:
            fallback_model = _env("ANTHROPIC_MODEL_SMALL_EDIT", "claude-sonnet-4-5")
            if fallback_model and fallback_model != model:
                retry = requests.post(
                    ANTHROPIC_MESSAGES_URL,
                    headers=headers,
                    json={
                        "model": fallback_model,
                        "max_tokens": max_tokens,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    timeout=90,
                )
                if retry.ok:
                    response = retry
                else:
                    return {
                        "ok": False,
                        "error": f"Anthropic API error ({retry.status_code}) with fallback model {fallback_model}: {retry.text[:700]}",
                    }
            else:
                return {"ok": False, "error": f"Anthropic API error ({response.status_code}): {response.text[:700]}"}
        else:
            return {"ok": False, "error": f"Anthropic API error ({response.status_code}): {response.text[:700]}"}

    payload = response.json() if response.text else {}
    text_chunks: List[str] = []
    for block in payload.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            text = str(block.get("text", "")).strip()
            if text:
                text_chunks.append(text)
    model_text = "\n".join(text_chunks).strip()
    if not model_text:
        return {"ok": False, "error": "Anthropic response contained no text."}
    return {"ok": True, "text": model_text}


def _write_log(entry: Dict[str, object]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=True) + "\n")


def _save_last_plan(payload: Dict[str, object]) -> None:
    LAST_PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_PLAN_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def _load_last_plan() -> Dict[str, object]:
    if not LAST_PLAN_PATH.exists():
        return {}
    try:
        payload = json.loads(LAST_PLAN_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _safe_workspace_file(path_text: str) -> Path:
    root = Path.cwd().resolve()
    candidate = Path(path_text).expanduser()
    if not candidate.is_absolute():
        candidate = (root / candidate).resolve()
    else:
        candidate = candidate.resolve()

    if root not in candidate.parents and candidate != root:
        raise RuntimeError(f"Path is outside workspace: {candidate}")
    if candidate == root:
        raise RuntimeError("Target path cannot be workspace root")
    return candidate


def _apply_insert_line(path: Path, line_number: int, content: str) -> Dict[str, object]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    idx = max(0, min(int(line_number) - 1, len(lines)))
    lines.insert(idx, str(content))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"path": str(path), "operation": "insert_line", "line": idx + 1}


def _apply_replace_text(path: Path, old_text: str, new_text: str) -> Dict[str, object]:
    content = path.read_text(encoding="utf-8", errors="replace")
    if old_text not in content:
        raise RuntimeError(f"replace_text failed: old_text not found in {path}")
    updated = content.replace(old_text, new_text, 1)
    path.write_text(updated, encoding="utf-8")
    return {"path": str(path), "operation": "replace_text"}


def _apply_append_text(path: Path, content: str) -> Dict[str, object]:
    existing = ""
    if path.exists():
        existing = path.read_text(encoding="utf-8", errors="replace")
    if existing and not existing.endswith("\n"):
        existing += "\n"
    path.write_text(existing + str(content), encoding="utf-8")
    return {"path": str(path), "operation": "append_text"}


def _execute_edits(plan_payload: Dict[str, object]) -> Dict[str, object]:
    plan = plan_payload.get("plan") if isinstance(plan_payload.get("plan"), dict) else {}
    edits = plan.get("edits", []) if isinstance(plan, dict) else []
    if not isinstance(edits, list) or not edits:
        return {"status": "error", "error": "No executable edits found in latest plan."}

    applied = []
    skipped = []
    for item in edits:
        if not isinstance(item, dict):
            skipped.append({"reason": "Invalid edit item format in plan."})
            continue

        path_text = str(item.get("path", "")).strip()
        action = str(item.get("action", "")).strip().lower()
        change = item.get("change")
        if not path_text:
            skipped.append({"path": "", "reason": "Edit is missing target path."})
            continue

        target = _safe_workspace_file(path_text)
        if not target.exists() and action != "append":
            skipped.append({"path": str(target), "reason": "Target file does not exist."})
            continue

        if isinstance(change, dict):
            change_type = str(change.get("type", "")).strip().lower()
            if change_type == "insert_line":
                result = _apply_insert_line(
                    target,
                    int(change.get("line_number", 1) or 1),
                    str(change.get("content", "")),
                )
                applied.append(result)
                continue
            if change_type == "replace_text":
                result = _apply_replace_text(
                    target,
                    str(change.get("old_text", "")),
                    str(change.get("new_text", "")),
                )
                applied.append(result)
                continue
            if change_type == "append_text":
                result = _apply_append_text(target, str(change.get("content", "")))
                applied.append(result)
                continue

        # Non-structured change payloads are not safely executable.
        skipped.append({"path": str(target), "reason": "Non-executable edit instructions."})

    if applied:
        result = {"status": "ok", "applied": applied}
        if skipped:
            result["skipped"] = skipped
        return result

    if skipped:
        return {
            "status": "error",
            "error": (
                "No executable edits could be applied. "
                "Ask for structured edits using change.type insert_line/replace_text/append_text and only existing files."
            ),
            "skipped": skipped,
        }

    return {"status": "error", "error": "No executable edits could be applied."}


def self_update_plan(instruction: str, target_files: Optional[List[str]] = None, scope: str = "auto") -> Dict[str, object]:
    instruction_text = (instruction or "").strip()
    files = list(target_files or [])
    if not instruction_text:
        return {"status": "error", "error": "Instruction is required."}

    resolved_scope = _choose_scope(instruction_text, files, scope)
    model = _choose_model(resolved_scope)
    file_context = _collect_file_context(files)

    if resolved_scope == "small_edit":
        operation_rules = (
            "For this small_edit request, edits must use only change.type insert_line or append_text. "
            "Do not use replace_text."
        )
    else:
        operation_rules = (
            "For full_rewrite planning, you may use insert_line, append_text, or replace_text when needed."
        )

    prompt = (
        "You are preparing a safe code update plan for an autonomous updater.\n"
        "Return only JSON object with keys: summary, scope, risk_level, edits, test_plan, notes.\n"
        "edits must be an array of objects: {path, action, rationale, change}.\n"
        "For machine execution, change MUST be one of:\n"
        "- {type:'insert_line', line_number:<1-based int>, content:<string>}\n"
        "- {type:'replace_text', old_text:<string>, new_text:<string>}\n"
        "- {type:'append_text', content:<string>}\n"
        f"{operation_rules}\n"
        "If target_files are provided, use only those existing files and do not invent new file paths.\n"
        "Do not include markdown fences.\n\n"
        f"Instruction:\n{instruction_text}\n\n"
        f"Scope: {resolved_scope}\n"
        f"Target files context JSON:\n{json.dumps(file_context, ensure_ascii=True)}"
    )

    result = _call_anthropic(model=model, prompt=prompt)
    timestamp = datetime.now(timezone.utc).isoformat()
    if not result.get("ok"):
        record = {
            "timestamp": timestamp,
            "instruction": instruction_text,
            "scope": resolved_scope,
            "model": model,
            "target_files": files,
            "status": "error",
            "error": result.get("error", "Unknown error"),
        }
        _write_log(record)
        return {"status": "error", "error": record["error"], "scope": resolved_scope, "model": model}

    model_text = str(result.get("text", ""))
    parsed = _extract_json_object(model_text)
    response_payload = {
        "status": "ok",
        "scope": resolved_scope,
        "model": model,
        "instruction": instruction_text,
        "target_files": files,
        "raw_response": model_text,
        "plan": parsed,
    }

    _save_last_plan(response_payload)

    _write_log(
        {
            "timestamp": timestamp,
            "instruction": instruction_text,
            "scope": resolved_scope,
            "model": model,
            "target_files": files,
            "status": "ok",
            "parsed": bool(parsed),
        }
    )
    return response_payload


def self_update(instruction: str, target_files: Optional[List[str]] = None, scope: str = "auto") -> str:
    """Queue a self-update request with model-tier selection and plan generation."""
    payload = self_update_plan(instruction=instruction, target_files=target_files, scope=scope)
    if payload.get("status") != "ok":
        return f"Self-update planning failed: {payload.get('error', 'unknown error')}"
    return (
        f"Self-update plan generated using {payload.get('model')} ({payload.get('scope')}). "
        "Review logs/update_requests.jsonl and the returned plan before applying changes."
    )


def self_update_execute_latest() -> Dict[str, object]:
    payload = _load_last_plan()
    if not payload:
        return {"status": "error", "error": "No saved plan found. Run self-update plan first."}

    scope = str(payload.get("scope", "")).strip().lower()
    if scope != "small_edit":
        return {
            "status": "error",
            "error": "Automatic execution is only enabled for small_edit plans.",
            "scope": scope,
        }

    result = _execute_edits(payload)
    timestamp = datetime.now(timezone.utc).isoformat()
    _write_log(
        {
            "timestamp": timestamp,
            "status": result.get("status", "error"),
            "event": "execute_latest_plan",
            "scope": scope,
            "details": result,
        }
    )
    return result
