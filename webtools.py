"""Web tools module for Future AI assistant - handles file operations, code generation, and system integrations."""
import os
import re
import subprocess
import json
import webbrowser
import sys
import urllib.parse
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import requests
import platform

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

from tools.integrations import (
    handle_calendar_command,
    handle_gmail_command,
    handle_spotify_command,
    has_pending_calendar_draft,
    should_handle_calendar_followup,
)
from updater import self_update_execute_latest, self_update_plan
from tools.search import local_search
from tools.anycubic import fetch_print_status
from tools.personality import apply_personality, load_personality
from tools.memory import extract_facts, load_memory, recall_fact, remember, save_memory, search_memory

try:
    import config
except Exception:
    """Retrieve environment variable with optional default value, stripping whitespace."""
    config = None
    """Retrieve environment variable with optional default value, stripping whitespace."""


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


_ANSI_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
_FILE_EXT_BY_KIND = {
    "html": ".html",
    "htm": ".html",
    "python": ".py",
    "py": ".py",
    "javascript": ".js",
    "js": ".js",
    "typescript": ".ts",
    "ts": ".ts",
    "css": ".css",
    "json": ".json",
    "markdown": ".md",
    "md": ".md",
    "text": ".txt",
    "txt": ".txt",
}

_FUSION_EXECUTABLE_CANDIDATES = [
    r"C:\Program Files\Autodesk\Fusion 360\Fusion360.exe",
    r"C:\Users\%USERNAME%\AppData\Local\Autodesk\webdeploy\production\Fusion360.exe",
]
_PENDING_SELF_UPDATE_VERIFICATION = None

def _discover_fusion_in_webdeploy() -> Optional[str]:
    """Discover Fusion 360 installation in webdeploy directory, returns most recent executable path."""
    configured_root = _env("FUSION360_WEBDEPLOY_ROOT")
    if configured_root:
        roots = [Path(configured_root).expanduser()]
    else:
        user_profile = os.getenv("USERPROFILE", "").strip()
        if not user_profile:
            return None
        roots = [Path(user_profile) / "AppData" / "Local" / "Autodesk" / "webdeploy" / "production"]

    candidates = []
    for root in roots:
        try:
            if not root.exists() or not root.is_dir():
                continue
            for exe in root.rglob("Fusion360.exe"):
                try:
                    if exe.is_file():
                        stat = exe.stat()
                        candidates.append((stat.st_mtime, str(exe.resolve())))
                except Exception:
                    continue
        except Exception:
            continue

    if not candidates:
        return None
    candidates.sort(key=lambda row: row[0], reverse=True)
    return candidates[0][1]


def _clean_model_text(text: str) -> str:
    cleaned = _ANSI_RE.sub("", text or "")
    cleaned = cleaned.replace("\r", "")

    def _looks_like_real_code_block(candidate: str) -> bool:
        body = (candidate or "").strip()
        if not body:
            return False

        lines = [line.strip() for line in body.split("\n") if line.strip()]
        if not lines:
            return False

        strong_line_patterns = [
            r"^def\s+\w+\s*\(",
            r"^class\s+\w+(?:\([^)]*\))?\s*:",
            r"^class\s+\w+(?:\s+extends\s+\w+)?\s*\{",
            r"^function\s+\w+\s*\(",
            r"^(?:const|let|var)\s+\w+\s*=",
            r"^from\s+[a-zA-Z0-9_.]+\s+import\s+",
            r"^import\s+[a-zA-Z0-9_.]+(?:\s+as\s+[a-zA-Z0-9_]+)?$",
            r"^#include\s*<",
            r"^<!doctype\s+html",
            r"^<html[\s>]",
        ]

        strong_matches = 0
        for line in lines:
            if any(re.search(pattern, line, re.IGNORECASE) for pattern in strong_line_patterns):
                strong_matches += 1

        if strong_matches >= 2:
            return True

        if strong_matches >= 1 and len(lines) >= 2:
            punctuation_heavy = sum(1 for line in lines if re.search(r"[{};]", line))
            if punctuation_heavy >= 1:
                return True

        return False

    # Sometimes the model wraps plain conversational text in a fenced block.
    # If it does not look like actual code, unwrap it for normal chat rendering.
    single_fence = re.fullmatch(r"\s*```[a-zA-Z0-9_+-]*\n([\s\S]*?)\n```\s*", cleaned)
    if single_fence:
        fenced_body = single_fence.group(1).strip("\n")
        looks_like_real_code = _looks_like_real_code_block(fenced_body)
        if not looks_like_real_code:
            cleaned = fenced_body

    code_hint = bool(re.search(r"```", cleaned)) or _looks_like_real_code_block(cleaned)
    if code_hint:
        lines = [re.sub(r"[ \t]+$", "", line) for line in cleaned.split("\n")]
        cleaned = "\n".join(lines)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    cleaned = re.sub(r"\n+", " ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip()

def _workspace_root() -> Path:
    configured = _env("FUTURE_FILES_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.cwd().resolve()


def _safe_target_file(path_text: str) -> Path:
    """Convert arbitrary text to safe filesystem name by replacing invalid characters."""
    root = _workspace_root()
    candidate = (root / path_text.strip().lstrip("/")).resolve()
    if root not in candidate.parents and candidate != root:
        raise ValueError("Path is outside the allowed workspace root")
    if candidate == root:
        raise ValueError("Target file path cannot be the workspace root")
    return candidate


def _slugify_name(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", (value or "").strip()).strip("._-")
    return cleaned or "future_output"


def _extract_requested_filename(query: str) -> Optional[str]:
    patterns = [
        r"(?:called|named|as)\s+([a-zA-Z0-9_./-]+\.[a-zA-Z0-9]+)",
        r"(?:file|document)\s+([a-zA-Z0-9_./-]+\.[a-zA-Z0-9]+)",
        r"save\s+(?:this|that|it)\s+as\s+([a-zA-Z0-9_./-]+\.[a-zA-Z0-9]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, query, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def _infer_extension(query: str) -> str:
    lowered = query.lower()
    if "." in lowered:
        explicit = re.search(r"\.[a-z0-9]{1,5}\b", lowered)
        if explicit:
            return explicit.group(0)
    for kind, ext in _FILE_EXT_BY_KIND.items():
        if re.search(rf"\b{re.escape(kind)}\b", lowered):
            return ext
    return ".txt"


def _extract_inline_code(query: str) -> str:
    fenced = re.search(r"```[a-zA-Z0-9_+-]*\n([\s\S]*?)```", query)
    if fenced:
        return fenced.group(1).strip("\n")
    parts = re.split(r"(?:with this code|code:|content:)", query, flags=re.IGNORECASE)
    if len(parts) > 1:
        return parts[-1].strip()
    return ""


def _extract_code_from_model_output(text: str) -> str:
    if not text:
        return ""
    fenced = re.search(r"```[a-zA-Z0-9_+-]*\n([\s\S]*?)```", text)
    if fenced:
        return fenced.group(1).strip("\n")
    return text.strip()


def _generate_file_content_with_model(query: str, ext: str, filename: str) -> str:
    if not _client:
        return ""

    language = {
        ".html": "HTML",
        ".py": "Python",
        ".js": "JavaScript",
        ".ts": "TypeScript",
        ".css": "CSS",
        ".json": "JSON",
        ".md": "Markdown",
    }.get(ext.lower(), "text")

    prompt = (
        f"Create complete, runnable {language} file content for this request: {query}\n"
        f"Target filename: {filename}\n"
        "Return only the file contents, no commentary."
    )

    model_candidates = []
    for candidate in [PRIMARY_MODEL, BACKUP_MODEL, "gpt-5", "gpt-4.1"]:
        name = (candidate or "").strip()
        if name and name not in model_candidates:
            model_candidates.append(name)

    for model_name in model_candidates:
        try:
            response = _client.chat.completions.create(
                model=model_name,
                messages=[
                    {
                        "role": "system",
                        "content": "You generate practical runnable code files. Output only file content.",
                    },
                    {"role": "user", "content": prompt},
                ],
                max_completion_tokens=1200,
            )
            text = (response.choices[0].message.content or "").strip()
            extracted = _extract_code_from_model_output(text)
            if extracted:
                return extracted
        except Exception as exc:
            print(f"File generation model error with {model_name}: {exc}")

    return ""


def _default_content_for_extension(ext: str, query: str) -> str:
    title = "Future Generated File"
    topic_match = re.search(r"for\s+([a-zA-Z0-9 _-]{3,})", query, re.IGNORECASE)
    if topic_match:
        title = topic_match.group(1).strip().strip(".?!")

    if ext == ".html":
        return (
            "<!DOCTYPE html>\n"
            "<html lang=\"en\">\n"
            "<head>\n"
            "  <meta charset=\"UTF-8\" />\n"
            "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />\n"
            f"  <title>{title}</title>\n"
            "</head>\n"
            "<body>\n"
            f"  <h1>{title}</h1>\n"
            "</body>\n"
            "</html>\n"
        )
    if ext == ".py":
        return (
            "def main():\n"
            f"    print(\"{title}\")\n\n"
            "if __name__ == \"__main__\":\n"
            "    main()\n"
        )
    if ext == ".js":
        return f"console.log(\"{title}\");\n"
    if ext == ".css":
        return "body {\n  margin: 0;\n  font-family: sans-serif;\n}\n"
    if ext == ".json":
        return json.dumps({"title": title}, indent=2) + "\n"
    if ext == ".md":
        return f"# {title}\n\nGenerated by Future.\n"
    return f"{title}\n"


def _is_file_creation_request(query: str) -> bool:
    lowered = query.lower()
    if not ("file" in lowered or "save this" in lowered or "save as" in lowered or re.search(r"\.[a-z0-9]{1,5}\b", lowered)):
        return False
    return any(keyword in lowered for keyword in ["create", "make", "generate", "write", "save"])


def _handle_file_creation_intent(query: str) -> Optional[str]:
    if not _is_file_creation_request(query):
        return None

    ext = _infer_extension(query)
    requested_name = _extract_requested_filename(query)
    overwrite = bool(re.search(r"\b(overwrite|replace)\b", query, re.IGNORECASE))

    if requested_name:
        filename = requested_name
    else:
        base = _slugify_name(re.sub(r"\b(create|make|generate|write|file|html|python|javascript|js|css|json|markdown|text|save|as|called|named)\b", " ", query, flags=re.IGNORECASE))
        filename = f"{base[:40] or 'future_output'}{ext}"

    if "." not in Path(filename).name:
        filename = f"{filename}{ext}"

    target = _safe_target_file(filename)
    if target.exists() and not overwrite:
        return f"{target.name} already exists. Say overwrite {target.name} to replace it."

    content = _extract_inline_code(query)
    if not content:
        content = _generate_file_content_with_model(query, target.suffix.lower(), target.name)
    if not content:
        content = _default_content_for_extension(target.suffix.lower(), query)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    rel = target.relative_to(_workspace_root()).as_posix()
    return f"Created {rel} ({len(content.encode('utf-8'))} bytes)."


def _printer_status_reply() -> str:
    try:
        status = fetch_print_status()
        if status.get("printing"):
            return (
                f"Printer is active. File: {status.get('file', 'Unknown')}. "
                f"Layer {status.get('layer', 0)} of {status.get('layer_total', 0)}. "
                f"Progress {status.get('progress_pct', 0)}%. "
                f"ETA {status.get('eta', '--')}. "
                f"Nozzle {status.get('nozzle_temp', 0)} degrees."
            )
        return "Printer is currently idle."
    except Exception as exc:
        fallback_path = Path(__file__).resolve().parent / "data" / "anycubic_status.json"
        if fallback_path.exists():
            try:
                payload = json.loads(fallback_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    if payload.get("printing"):
                        return (
                            f"Printer status (cached): active. File: {payload.get('file', 'Unknown')}. "
                            f"Layer {payload.get('layer', 0)} of {payload.get('layer_total', 0)}. "
                            f"Progress {payload.get('progress_pct', 0)}%. ETA {payload.get('eta', '--')}."
                        )
                    return "Printer status (cached): idle."
            except Exception:
                pass
        if "ANYCUBIC_BASE_URL is not configured" in str(exc):
            return (
                "Printer telemetry is not connected yet. Add ANYCUBIC_BASE_URL and ANYCUBIC_BACKEND "
                "to .env, then I can report live print status."
            )
        return (
            "I could not read live printer status yet. Set ANYCUBIC_BASE_URL and ANYCUBIC_BACKEND "
            "in .env to enable live printer telemetry."
        )


def _looks_like_calendar_intent(query_lower: str) -> bool:
    if any(k in query_lower for k in ["calendar", "calender", "schedule", "event", "appointment", "remind", "reminder"]):
        return True

    natural_lookup_phrases = [
        "what do i have",
        "what's on",
        "whats on",
        "going on today",
        "going on tomorrow",
        "am i busy",
        "my day look like",
        "upcoming today",
        "plans today",
    ]
    return any(phrase in query_lower for phrase in natural_lookup_phrases)


def _looks_like_gmail_intent(query_lower: str) -> bool:
    if any(k in query_lower for k in ["gmail", "email", "mail", "inbox"]):
        return True

    natural_lookup_phrases = [
        "unread messages",
        "unread email",
        "new email",
        "new messages",
        "check inbox",
        "did i get any emails",
        "did i get any email",
        "did anyone email me",
    ]
    return any(phrase in query_lower for phrase in natural_lookup_phrases)


def _looks_like_spotify_intent(query_lower: str) -> bool:
    explicit_keywords = ["spotify", "song", "music", "playlist", "track", "album", "artist"]
    if any(k in query_lower for k in explicit_keywords):
        return True

    control_phrases = ["pause", "resume", "skip", "next song", "previous song", "volume", "turn it up", "turn it down"]
    if any(phrase in query_lower for phrase in control_phrases):
        return True

    if re.search(r"\bplay\s+(?:some\s+)?(?:music|song|songs|spotify|playlist|track|tracks|album|artist)\b", query_lower):
        return True
    if query_lower.startswith("play ") and not any(token in query_lower for token in ["game", "video", "movie"]):
        return True

    return False


def _looks_like_printer_intent(query_lower: str) -> bool:
    if any(k in query_lower for k in ["print", "printer", "pritner", "slicer", "anycubic", "kobra", "nozzle", "layer"]):
        return True

    natural_phrases = [
        "anything printing",
        "printing right now",
        "print status",
        "3d printer",
        "how is the print",
    ]
    return any(phrase in query_lower for phrase in natural_phrases)


def _looks_like_fusion_intent(query_lower: str) -> bool:
    return any(
        phrase in query_lower
        for phrase in [
            "fusion",
            "fusion 360",
            "autodesk",
            "prosthetic hand",
            "connect fusion",
            "authorize fusion",
        ]
    )


def _looks_like_self_update_intent(query_lower: str) -> bool:
    if re.match(r"^update(?:\s+(?:my|your|the)\s+(?:code|assistant|app|project|config|feature|features)|\s+code|\s+my\s+code|\s+your\s+code|\s+the\s+code)?$", query_lower):
        return True
    return any(
        phrase in query_lower
        for phrase in [
            "self update",
            "self-update",
            "update yourself",
            "run updater",
            "update plan",
            "edit my code",
            "edit your code",
            "modify my code",
            "change my code",
            "fix my code",
            "code edits",
            "code updates",
            "code update",
            "code changes",
            "apply changes",
            "make changes",
            "use vscode",
            "use vs code",
            "vs code interface",
            "vscode interface",
            "wrapper",
        ]
    )


def _looks_like_self_update_execute_intent(query_lower: str) -> bool:
    execute_markers = [
        "execute the update",
        "apply the update",
        "run the update",
        "execute update",
        "apply update",
        "finish the update",
        "execute it",
        "apply it",
        "run it",
    ]
    return any(marker in query_lower for marker in execute_markers)


def _looks_like_self_update_direct_confirm(query_lower: str) -> bool:
    confirm_markers = [
        "okay run it",
        "ok run it",
        "go ahead",
        "do it",
        "proceed",
        "carry on",
        "run it",
    ]
    return any(marker in query_lower for marker in confirm_markers)


def _looks_like_basic_code_rerun_intent(query_lower: str) -> bool:
    markers = [
        "basic code rerun",
        "code rerun",
        "rerun the code",
        "re-run the code",
        "rerun code",
        "re-run code",
        "smoke test",
        "basic smoke test",
        "code check",
    ]
    return any(marker in query_lower for marker in markers)


def _looks_like_self_update_verification_intent(query_lower: str) -> bool:
    return bool(re.search(r"\b(?:verify|confirm|approve)\b", query_lower)) and "update" in query_lower


def _self_update_requires_verification() -> bool:
    return _env("FUTURE_SELF_UPDATE_REQUIRE_VERIFICATION", "1").lower() in {"1", "true", "yes", "on"}


def _start_self_update_verification() -> str:
    global _PENDING_SELF_UPDATE_VERIFICATION
    code = str(secrets.randbelow(900000) + 100000)
    _PENDING_SELF_UPDATE_VERIFICATION = {
        "code": code,
        "expires_at": time.time() + 600,
    }
    return (
        "Verification required before execution. "
        f"Reply with: verify update {code}"
    )


def _extract_verification_code(query: str) -> str:
    match = re.search(r"\b(?:verify|confirm|approve)\s+(?:the\s+)?(?:update\s+)?([0-9]{4,8})\b", query, flags=re.I)
    if match:
        return match.group(1).strip()
    return ""


def _handle_self_update_verification(query: str) -> str:
    global _PENDING_SELF_UPDATE_VERIFICATION
    if not isinstance(_PENDING_SELF_UPDATE_VERIFICATION, dict):
        return "No pending update execution request. Say execute the update first."

    if time.time() > float(_PENDING_SELF_UPDATE_VERIFICATION.get("expires_at", 0)):
        _PENDING_SELF_UPDATE_VERIFICATION = None
        return "Verification expired. Say execute the update again for a new code."

    code = _extract_verification_code(query)
    if not code:
        return "Please reply in this format: verify update <code>."

    expected = str(_PENDING_SELF_UPDATE_VERIFICATION.get("code", "")).strip()
    if code != expected:
        return "Verification code did not match. Please try again."

    _PENDING_SELF_UPDATE_VERIFICATION = None
    return _self_update_execute_reply()


def _self_update_scope_from_query(query_lower: str) -> str:
    if any(token in query_lower for token in ["full rewrite", "rewrite", "overhaul", "major refactor"]):
        return "full_rewrite"
    if any(token in query_lower for token in ["small edit", "small code", "minor edit", "quick edit", "one-line", "one line"]):
        return "small_edit"
    return "auto"


def _extract_target_files(query: str):
    pattern = r"\b[a-zA-Z0-9_./-]+\.(?:py|js|ts|tsx|html|css|json|md|yaml|yml)\b"
    seen = []
    for match in re.findall(pattern, query):
        item = match.strip()
        if item and item not in seen:
            seen.append(item)
    return seen


def _default_self_update_target_files(query_lower: str):
    if any(token in query_lower for token in ["voice", "tts", "eleven", "elevenlabs", "male", "female"]):
        return ["webtools.py", "api_server.py", "dashboard.html", "main.py", "config.py"]
    if any(token in query_lower for token in ["calendar", "event", "schedule"]):
        return ["tools/integrations.py", "webtools.py", "api_server.py"]
    if any(token in query_lower for token in ["fusion", "autodesk"]):
        return ["webtools.py", "api_server.py"]
    return ["webtools.py", "api_server.py"]


def _self_update_instruction(query: str) -> str:
    cleaned = re.sub(r"\b(run\s+)?self[-\s]?update\b\s*[:\-]?", "", query, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"\bupdate\s+plan\b\s*[:\-]?", "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned or query.strip()


def _self_update_reply(query: str) -> str:
    query_lower = query.lower()
    scope = _self_update_scope_from_query(query_lower)
    target_files = _extract_target_files(query)
    if not target_files:
        target_files = _default_self_update_target_files(query_lower)
    instruction = _self_update_instruction(query)

    result = self_update_plan(instruction=instruction, target_files=target_files, scope=scope)
    if result.get("status") != "ok":
        return f"Self-update planner failed: {result.get('error', 'unknown error')}"

    plan = result.get("plan") if isinstance(result.get("plan"), dict) else {}
    summary = str(plan.get("summary", "Plan created.")).strip() if plan else "Plan created."
    risk = str(plan.get("risk_level", "unknown")).strip() if plan else "unknown"
    model = str(result.get("model", "")).strip() or "configured model"
    resolved_scope = str(result.get("scope", scope)).strip()

    if resolved_scope == "small_edit":
        execute_result = self_update_execute_latest()
        if execute_result.get("status") == "ok":
            applied = execute_result.get("applied", [])
            if not isinstance(applied, list):
                applied = []
            skipped = execute_result.get("skipped", [])
            skipped_count = len(skipped) if isinstance(skipped, list) else 0
            touched = ", ".join(str(item.get("path", "")) for item in applied[:5] if isinstance(item, dict) and item.get("path"))
            base = f"Self-update executed successfully ({resolved_scope}, {model})."
            if touched:
                base += f" Applied {len(applied)} edit(s): {touched}."
            if skipped_count:
                base += f" Skipped {skipped_count} non-executable edit(s)."
            return base

        return (
            f"Self-update plan created ({resolved_scope}, {model}) but execution failed: "
            f"{execute_result.get('error', 'unknown error')}"
        )

    return (
        f"Self-update plan ready ({resolved_scope}, {model}). "
        f"Summary: {summary} Risk: {risk}. "
        "I logged the request in logs/update_requests.jsonl."
    )


def _self_update_execute_reply() -> str:
    result = self_update_execute_latest()
    if result.get("status") != "ok":
        skipped = result.get("skipped", [])
        skipped_count = len(skipped) if isinstance(skipped, list) else 0
        suffix = f" Skipped {skipped_count} non-executable edit(s)." if skipped_count else ""
        return f"Self-update execute failed: {result.get('error', 'unknown error')}.{suffix}"

    applied = result.get("applied", [])
    if not isinstance(applied, list):
        applied = []
    skipped = result.get("skipped", [])
    skipped_count = len(skipped) if isinstance(skipped, list) else 0
    touched = ", ".join(str(item.get("path", "")) for item in applied[:5] if isinstance(item, dict) and item.get("path"))
    if touched:
        suffix = f" Skipped {skipped_count} non-executable edit(s)." if skipped_count else ""
        return f"Self-update executed successfully. Applied {len(applied)} edit(s): {touched}.{suffix}"
    suffix = f" Skipped {skipped_count} non-executable edit(s)." if skipped_count else ""
    return f"Self-update executed successfully.{suffix}"


def _basic_code_rerun_reply() -> str:
    files = ["main.py", "webtools.py", "api_server.py", "tools/integrations.py"]
    root = _workspace_root()
    missing = [name for name in files if not (root / name).exists()]
    if missing:
        return f"Basic code rerun could not start because these files are missing: {', '.join(missing)}."

    cmd = [sys.executable, "-m", "py_compile", *files]
    try:
        result = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=60)
    except Exception as exc:
        return f"Basic code rerun failed to start: {exc}"

    if result.returncode == 0:
        return "Basic code rerun passed. Python syntax compiled cleanly for the core app files."

    stderr = (result.stderr or result.stdout or "").strip()
    if len(stderr) > 700:
        stderr = stderr[:700]
    return f"Basic code rerun failed. {stderr or 'Python compilation returned a non-zero exit code.'}"


def _self_update_wrapper_reply(query: str) -> str:
    query_lower = query.lower()
    if query_lower.strip() == "update":
        return (
            "Tell me what to update: code/features, a specific file, or say 'execute the update' if a small_edit plan is already saved."
        )

    if any(phrase in query_lower for phrase in ["what exactly is preventing", "why can't you", "why cant you", "what is preventing", "blocked by", "limiting your ability"]):
        return (
            "The blocker is usually the local tool path, not a different LLM. "
            "When a request doesn't match the self-update router, it falls back to normal chat and the model explains its own limitations. "
            "Use the self-update wrapper or ask for an explicit code update, and I can plan it or execute a saved small_edit."
        )

    if any(phrase in query_lower for phrase in ["vscode interface", "vs code interface", "wrapper", "wrapper like that", "use vscode", "use vs code"]):
        vscode_files = ["webtools.py", "updater.py", "api_server.py"]
        links = []
        root = _workspace_root()
        for name in vscode_files:
            target = root / name
            if target.exists():
                links.append(_generate_vscode_uri(str(target), 1))
        link_text = "\n".join(f"- {link}" for link in links) if links else "- no openable files found"
        return (
            "Yes. The wrapper should live above the LLM: it should catch code-update intent, generate a structured plan, and route execution locally. "
            "Open these files in VS Code to inspect the current wrapper path:\n"
            f"{link_text}\n"
            "If you want, I can wire a stricter VS Code-first update wrapper next so code-edit requests never fall through to normal chat."
        )

    return _self_update_reply(query)


def _looks_like_rewrite_planning_intent(query_lower: str) -> bool:
    rewrite_terms = ["rewrite", "refactor", "overhaul", "rebuild", "rewrite plan", "code rewrite"]
    scope_terms = ["code", "project", "app", "system", "future", "yourself", "your code"]
    return any(term in query_lower for term in rewrite_terms) and any(term in query_lower for term in scope_terms)


def _rewrite_planning_clarifier() -> str:
    return (
        "Yes, I can help plan the rewrite. Before I propose a path, I need these details first: "
        "1) What exactly are we upgrading (specific module or feature)? "
        "2) Is this a cleanup/refactor or a behavior-changing rebuild? "
        "3) Which model should power chat after the rewrite (gpt-5, gpt-4.1, or claude-sonnet-4-5)? "
        "4) What must stay unchanged (APIs, commands, voice flow, dashboard behavior)? "
        "5) Do you want a phased migration or one-shot rewrite?"
    )


def _resolve_fusion_executable() -> Optional[str]:
    env_path = _env("FUSION360_PATH")
    if env_path and Path(env_path).exists():
        return env_path

    discovered = _discover_fusion_in_webdeploy()
    if discovered:
        return discovered

    for candidate in _FUSION_EXECUTABLE_CANDIDATES:
        expanded = os.path.expandvars(candidate)
        if Path(expanded).exists():
            return expanded
    return None


def _open_fusion_desktop() -> str:
    fusion_path = _resolve_fusion_executable()
    if not fusion_path:
        return (
            "I could not find Fusion 360 on this PC. Set FUSION360_PATH in .env to your Fusion360.exe path, "
            "then say 'open fusion' again."
        )

    try:
        os.startfile(fusion_path)
    except Exception:
        try:
            subprocess.Popen([fusion_path])
        except Exception as exc:
            return f"I found Fusion 360, but could not open it: {exc}"
    return "Opening Fusion 360 now."


def _fusion_open_file_name_from_query(query: str) -> str:
    patterns = [
        r"open\s+(.+?)\s+in\s+fusion",
        r"open\s+fusion\s+file\s+(.+)",
        r"load\s+(.+?)\s+from\s+fusion",
    ]
    for pattern in patterns:
        match = re.search(pattern, query, re.IGNORECASE)
        if match:
            return match.group(1).strip(" .?!\"")
    return ""


def _fusion_command_reply(query: str) -> str:
    q = query.lower().strip()
    api_base = _env("FUTURE_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

    if any(token in q for token in ["connect fusion", "authorize fusion", "login fusion", "sign in fusion", "oauth fusion"]):
        def _direct_oauth_url() -> str:
            client_id = _env("AUTODESK_CLIENT_ID")
            redirect_uri = _env("AUTODESK_REDIRECT_URI", "http://localhost:8000/callback/autodesk")
            scopes = _env("AUTODESK_SCOPES", "data:read")
            if not client_id:
                return ""
            params = {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": scopes,
                "state": "future-local-oauth",
            }
            return "https://developer.api.autodesk.com/authentication/v2/authorize?" + urllib.parse.urlencode(params)

        try:
            response = requests.post(f"{api_base}/api/connect/fusion360", timeout=20)
            payload = response.json() if response.ok and response.text else {}
            auth_url = str(payload.get("auth_url", "")).strip()
            if not auth_url:
                auth_url = _direct_oauth_url()
            if not auth_url:
                return (
                    "Fusion connect failed because AUTODESK_CLIENT_ID is missing. "
                    "Add AUTODESK_CLIENT_ID and AUTODESK_CLIENT_SECRET to .env first."
                )
            try:
                webbrowser.open(auth_url)
            except Exception:
                pass
            return (
                "Fusion OAuth started. I opened the Autodesk sign-in page. "
                f"If it did not open, use this URL: {auth_url}"
            )
        except Exception as exc:
            auth_url = _direct_oauth_url()
            if auth_url:
                return (
                    "Fusion OAuth started with direct auth URL fallback. "
                    f"Open this URL: {auth_url}. Details: {exc}"
                )
            return f"Fusion connect failed: {exc}"

    if "fusion status" in q or "oauth status" in q:
        try:
            response = requests.get(f"{api_base}/api/fusion360/oauth/status", timeout=20)
            if not response.ok:
                return f"Fusion status check failed: {response.text[:240]}"
            payload = response.json() if response.text else {}
            if payload.get("connected"):
                user = payload.get("user", {}) or {}
                name = str(user.get("name", "")).strip() or "Connected user"
                return f"Fusion cloud is connected as {name}."
            return "Fusion cloud is not connected yet. Say 'connect fusion' to start OAuth."
        except Exception as exc:
            return f"Fusion status check failed: {exc}"

    requested_file = _fusion_open_file_name_from_query(query)
    if requested_file:
        try:
            response = requests.post(
                f"{api_base}/api/fusion360/open",
                json={"file_name": requested_file, "open_in_browser": True},
                timeout=40,
            )
            if not response.ok:
                return f"Fusion file open failed: {response.text[:240]}"
            payload = response.json() if response.text else {}
            if payload.get("found"):
                return (
                    f"Found and opened Fusion file '{payload.get('file', {}).get('name', requested_file)}' "
                    f"from project '{payload.get('project_name', 'Unknown')}'."
                )
            return (
                f"I could not find '{requested_file}' in Fusion cloud yet. "
                "Try 'search fusion prosthetic hand' after OAuth is connected."
            )
        except Exception as exc:
            return f"Fusion file open failed: {exc}"

    if q.startswith("search fusion ") or "find in fusion" in q:
        term = re.sub(r"^search\s+fusion\s+", "", query, flags=re.IGNORECASE).strip()
        term = re.sub(r"^find\s+in\s+fusion\s+", "", term, flags=re.IGNORECASE).strip() or "prosthetic hand"
        try:
            response = requests.get(
                f"{api_base}/api/fusion360/files/search",
                params={"query": term, "limit": 5},
                timeout=40,
            )
            if not response.ok:
                return f"Fusion search failed: {response.text[:240]}"
            rows = response.json() if response.text else []
            if not rows:
                return f"No Fusion cloud files matched '{term}'."
            preview = ", ".join(str(row.get("name", "Untitled")) for row in rows[:5])
            return f"I found these Fusion files: {preview}."
        except Exception as exc:
            return f"Fusion search failed: {exc}"

    if "open fusion" in q or "launch fusion" in q:
        return _open_fusion_desktop()

    return (
        "Fusion command recognized. You can say 'connect fusion', 'fusion status', "
        "'search fusion prosthetic hand', or 'open prosthetic hand in fusion'."
    )


if hasattr(config, "OPENAI_API_KEY") and getattr(config, "OPENAI_API_KEY"):
    OPENAI_API_KEY = _env("OPENAI_API_KEY") or _env("FUTURE_OPENAI_API_KEY") or str(getattr(config, "OPENAI_API_KEY"))
else:
    OPENAI_API_KEY = _env("OPENAI_API_KEY") or _env("FUTURE_OPENAI_API_KEY")

if hasattr(config, "ANTHROPIC_API_KEY") and getattr(config, "ANTHROPIC_API_KEY"):
    ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY") or _env("FUTURE_ANTHROPIC_API_KEY") or str(getattr(config, "ANTHROPIC_API_KEY"))
else:
    ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY") or _env("FUTURE_ANTHROPIC_API_KEY")

PRIMARY_MODEL = _env("PRIMARY_MODEL", getattr(config, "PRIMARY_MODEL", "gpt-4o-mini") if config else "gpt-4o-mini")
BACKUP_MODEL = _env("BACKUP_MODEL", getattr(config, "BACKUP_MODEL", "gpt-4o-mini") if config else "gpt-4o-mini")
ANTHROPIC_MODEL = _env("ANTHROPIC_MODEL", getattr(config, "ANTHROPIC_MODEL", "claude-sonnet-4-5") if config else "claude-sonnet-4-5")
ALLOW_OLLAMA_FALLBACK = _env("FUTURE_ALLOW_OLLAMA_FALLBACK", "0").lower() in {"1", "true", "yes", "on"}

_client: Optional[object] = None
if OpenAI and OPENAI_API_KEY:
    try:
        _client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as exc:
        print("OpenAI client init failed:", exc)
        _client = None


def _is_anthropic_model(model_name: str) -> bool:
    normalized = (model_name or "").strip().lower()
    return normalized.startswith("claude") or normalized.startswith("anthropic")


def _anthropic_reply(messages, model_name: str, max_tokens: int = 500) -> Optional[str]:
    if not ANTHROPIC_API_KEY:
        return None

    system_text = ""
    anthro_messages = []
    for item in messages or []:
        role = str(item.get("role", "")).strip().lower()
        content = str(item.get("content", ""))
        if role == "system":
            if content:
                system_text = content
            continue
        if role in {"user", "assistant"} and content:
            anthro_messages.append({"role": role, "content": content})

    if not anthro_messages:
        return None

    payload = {
        "model": model_name,
        "max_tokens": max_tokens,
        "messages": anthro_messages,
    }
    if system_text:
        payload["system"] = system_text

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    body = response.json() or {}

    pieces = []
    for block in body.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            text = str(block.get("text", "")).strip()
            if text:
                pieces.append(text)
    if not pieces:
        return None
    return _clean_model_text("\n".join(pieces))


def organize_notes_text(raw_text: str, existing_content: str = "") -> str:
    """Turn raw dictated/spoken text into organized, summarized notes (headings + bullets)."""
    raw_text = (raw_text or "").strip()
    if not raw_text:
        return existing_content or ""

    instructions = (
        "You are organizing spoken notes into clean written notes. "
        "Merge the new spoken material into the existing notes below, keeping everything still relevant. "
        "Summarize rambling speech into concise bullet points, group related ideas under short headings, "
        "fix obvious transcription errors, and drop filler words. "
        "Return only the final plain-text notes (markdown-style headings and bullets are fine), no commentary."
    )
    user_content = (
        f"Existing notes:\n{existing_content or '(empty)'}\n\n"
        f"New spoken material to add:\n{raw_text}"
    )
    messages = [
        {"role": "system", "content": instructions},
        {"role": "user", "content": user_content},
    ]

    model_candidates = []
    for candidate in [PRIMARY_MODEL, BACKUP_MODEL, ANTHROPIC_MODEL]:
        model_name = (candidate or "").strip()
        if model_name and model_name not in model_candidates:
            model_candidates.append(model_name)

    for model_name in model_candidates:
        try:
            if _is_anthropic_model(model_name):
                cleaned = _anthropic_reply(messages, model_name=model_name, max_tokens=1200)
            elif _client:
                response = _client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    max_completion_tokens=1200,
                )
                text = response.choices[0].message.content
                cleaned = _clean_model_text(text or "") if text else None
            else:
                cleaned = None
            if cleaned:
                return cleaned
        except Exception as exc:
            print(f"Notes organize error with model {model_name}: {exc}")

    # No model available: fall back to appending the raw text as-is.
    joined = f"{existing_content}\n\n{raw_text}" if existing_content else raw_text
    return joined.strip()


def _looks_like_content_creation_intent(query_lower: str) -> bool:
    return any(
        phrase in query_lower
        for phrase in [
            "activate content creation",
            "start content creation",
            "content creation automation",
            "make my instagram videos",
            "make the instagram videos",
            "generate instagram content",
            "start the content batch",
        ]
    )


def _start_content_creation_reply(query: str) -> str:
    from tools import tasks as background_tasks
    from tools.content_studio import _resolve_source_dir, scan_source_assets

    source_dir = _resolve_source_dir()
    if not source_dir:
        return (
            "I don't have a content source folder set up yet \u2014 set FUTURE_CONTENT_SOURCE_DIR to the "
            "folder with your clips/screenshots, then ask me again."
        )

    assets = scan_source_assets(source_dir)
    if not assets:
        return f"I checked {source_dir} but didn't find any images or clips to work with."

    task = background_tasks.enqueue("instagram_batch", {})
    return (
        f"Got it \u2014 found {len(assets)} source files. I'm scanning and building the video batch now "
        f"(task {task['id'][:8]}); I'll email you the full batch to review once it's done."
    )


def _handle_local_intents(query: str) -> Optional[str]:
    q = query.lower()

    if _looks_like_rewrite_planning_intent(q):
        return _rewrite_planning_clarifier()

    if _looks_like_content_creation_intent(q):
        return _start_content_creation_reply(query)

    if has_pending_calendar_draft() and should_handle_calendar_followup(query):
        try:
            result = handle_calendar_command(query)
            if result:
                return result
        except Exception as exc:
            return f"Calendar command failed: {exc}"

    if _looks_like_self_update_verification_intent(q):
        try:
            return _handle_self_update_verification(query)
        except Exception as exc:
            return f"Self-update verification failed: {exc}"

    if _looks_like_self_update_direct_confirm(q):
        try:
            return _self_update_execute_reply()
        except Exception as exc:
            return f"Self-update execute failed: {exc}"

    if _looks_like_self_update_execute_intent(q):
        try:
            if _self_update_requires_verification():
                return _start_self_update_verification()
            return _self_update_execute_reply()
        except Exception as exc:
            return f"Self-update execute failed: {exc}"

    if _looks_like_basic_code_rerun_intent(q):
        try:
            return _basic_code_rerun_reply()
        except Exception as exc:
            return f"Basic code rerun failed: {exc}"

    if _looks_like_self_update_intent(q):
        try:
            return _self_update_wrapper_reply(query)
        except Exception as exc:
            return f"Self-update command failed: {exc}"

    file_creation_reply = _handle_file_creation_intent(query)
    if file_creation_reply:
        return file_creation_reply

    wants_calendar = _looks_like_calendar_intent(q)
    wants_gmail = _looks_like_gmail_intent(q)
    wants_spotify = _looks_like_spotify_intent(q)
    wants_printer = _looks_like_printer_intent(q)
    wants_fusion = _looks_like_fusion_intent(q)

    selected_handlers = []
    if wants_printer:
        selected_handlers.append(("printer", lambda: _printer_status_reply()))
    if wants_fusion:
        selected_handlers.append(("fusion", lambda: _fusion_command_reply(query)))
    if wants_calendar:
        selected_handlers.append(("calendar", lambda: handle_calendar_command(query)))
    if wants_gmail:
        selected_handlers.append(("gmail", lambda: handle_gmail_command(query)))
    if wants_spotify:
        selected_handlers.append(("spotify", lambda: handle_spotify_command(query)))

    if selected_handlers:
        responses = []
        for name, handler in selected_handlers:
            try:
                result = handler()
                if result:
                    responses.append(result)
            except Exception as exc:
                responses.append(f"{name.capitalize()} command failed: {exc}")

        if responses:
            return " ".join(responses)

    if any(k in q for k in ["file", "folder", "find", "search my files"]):
        term = query.replace("search my files", "").strip() or query
        hits = local_search(term)
        if not hits:
            return "I could not find matching files."
        preview = ", ".join(hits[:5])
        more = "" if len(hits) <= 5 else f" (+{len(hits) - 5} more)"
        return f"I found these matches: {preview}{more}"

    return None


def _local_fallback_reply(query: str) -> str:
    return (
        "Cloud chat is not configured. Set OPENAI_API_KEY or ANTHROPIC_API_KEY and optionally "
        "PRIMARY_MODEL (for example gpt-5 or claude-sonnet-4-5) in your environment, then reload the API server."
    )


def _format_fact_response(fact) -> str:
    subject = str(fact.get("subject", "")).strip()
    value = str(fact.get("value", "")).strip()
    if not subject or not value:
        return ""

    lowered = subject.lower()
    if lowered.startswith("my "):
        return f"{subject[:1].upper()}{subject[1:]} is {value}."
    return f"Your {subject} is {value}."


def _handle_memory_intents(query: str) -> Optional[str]:
    memory = load_memory()
    lowered = query.lower().strip()

    # Prefer a deterministic status summary when the user explicitly asks to
    # check memory, so we avoid generic model-generated answers.
    snapshot_markers = [
        "check your memory",
        "check my memory",
        "memory check",
        "what is in your memory",
        "what's in your memory",
        "whats in your memory",
        "show your memory",
        "show me your memory",
        "did you get my memory",
        "new stuff",
    ]
    if "memory" in lowered and any(marker in lowered for marker in snapshot_markers):
        facts = extract_facts(memory)
        recent_items = [item for item in memory if str(item.get("user", "")).strip() or str(item.get("ai", "")).strip()][-3:]

        lines = [
            f"I can see {len(memory)} stored memory entries and {len(facts)} extracted facts.",
        ]

        if recent_items:
            lines.append("Most recent memory snippets:")
            for item in recent_items:
                user_text = str(item.get("user", "")).strip()
                ai_text = str(item.get("ai", "")).strip()
                if user_text and ai_text:
                    lines.append(f"- User: {user_text[:120]} | Future: {ai_text[:120]}")
                elif user_text:
                    lines.append(f"- User: {user_text[:160]}")
                elif ai_text:
                    lines.append(f"- Future: {ai_text[:160]}")

        lines.append("If you want, ask me a specific recall question and I will answer from saved memory.")
        return "\n".join(lines)

    remember_match = re.search(r"\bremember(?: that)?\s+(?P<fact>.+)", query, re.IGNORECASE)
    if remember_match:
        fact_text = remember_match.group("fact").strip(" .?!")
        reply = f"I'll remember that {fact_text}."
        remember(memory, query, reply)
        save_memory(memory)
        return reply

    recall_markers = [
        "what is my",
        "what's my",
        "whats my",
        "do you remember",
        "what do you remember",
        "what do you know about",
    ]
    if any(marker in lowered for marker in recall_markers):
        fact = recall_fact(memory, query)
        if fact:
            return _format_fact_response(fact)

    return None


def time_context(client_time: Optional[str] = None) -> str:
    """Describe the current moment (day/time/part-of-day) so replies can be time-aware."""
    now = None
    if client_time:
        try:
            normalized = str(client_time).strip().replace("Z", "+00:00")
            now = datetime.fromisoformat(normalized)
        except Exception:
            now = None
    if now is None:
        now = datetime.now()

    hour = now.hour
    if 5 <= hour < 12:
        part_of_day = "morning"
    elif 12 <= hour < 17:
        part_of_day = "afternoon"
    elif 17 <= hour < 22:
        part_of_day = "evening"
    else:
        part_of_day = "late night"

    descriptor = f"It is currently {now.strftime('%-I:%M %p')} on {now.strftime('%A')} ({part_of_day})." if os.name != "nt" else f"It is currently {now.strftime('%I:%M %p').lstrip('0')} on {now.strftime('%A')} ({part_of_day})."
    if part_of_day == "late night":
        descriptor += " It's late, so it's natural to bring up rest/sleep if it fits the conversation."
    return descriptor


def _build_chat_messages(query: str, recent_context=None, client_time: Optional[str] = None):
    personality = load_personality()
    memory = load_memory()
    history_lines = []
    relevant_memory = search_memory(memory, query, limit=6)
    recent_memory = memory[-4:]
    combined_memory = []
    seen = set()

    for item in relevant_memory + recent_memory:
        item_key = (str(item.get("user", "")), str(item.get("ai", "")))
        if item_key in seen:
            continue
        seen.add(item_key)
        combined_memory.append(item)

    for item in combined_memory:
        user_text = str(item.get("user", "")).strip()
        ai_text = str(item.get("ai", "")).strip()
        if user_text:
            history_lines.append(f"User: {user_text}")
        if ai_text:
            history_lines.append(f"Future: {ai_text}")

    facts = extract_facts(memory)
    fact_lines = [f"- {fact['subject']}: {fact['value']}" for fact in facts[-12:]]
    history_text = "\n".join(history_lines) if history_lines else "No stored conversations yet."
    fact_text = "\n".join(fact_lines) if fact_lines else "No stored facts yet."

    recent_lines = []
    for item in (recent_context or [])[-20:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "")).strip().lower()
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        if role == "user":
            recent_lines.append(f"User: {content}")
        elif role == "assistant":
            recent_lines.append(f"Future: {content}")

    recent_text = "\n".join(recent_lines) if recent_lines else "No recent chat turns."

    system_prompt = (
        f"You are {personality['name']}, a highly capable personal AI assistant. "
        f"Your traits are {personality['traits']} and your tone is {personality['tone']}. "
        "Use the stored conversation history as long-term memory about the user. "
        "Use the recent chat turns as short-term memory to resolve follow-up messages and context. "
        "If the user asks what you remember, answer from that memory when possible. "
        "Do not claim you cannot remember across chats when the stored memory includes relevant information. "
        "Default to concise, practical, and action-oriented answers, but if the user asks for depth, detail, or step-by-step explanation, provide a fuller long-form answer.\n\n"
        f"{time_context(client_time)}\n\n"
        f"Recent chat turns (last 20 lines):\n{recent_text}\n\n"
        f"Stored facts:\n{fact_text}\n\n"
        f"Stored conversation history:\n{history_text}"
    )
    return memory, [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": query},
    ]


def _reply_with_local_model(query: str) -> Optional[str]:
    try:
        personality = load_personality()
        memory = load_memory()
        response = apply_personality(query, personality, memory)
        cleaned = _clean_model_text(response or "")
        if not cleaned:
            return None
        remember(memory, query, cleaned)
        save_memory(memory)
        return cleaned
    except Exception as exc:
        print("Local model error:", exc)
        return None


def _reply_with_ollama_fallback(query: str) -> Optional[str]:
    model_candidates = []
    configured = _env("OLLAMA_MODEL", getattr(config, "OLLAMA_MODEL", "") if config else "")
    if configured:
        model_candidates.append(configured)

    try:
        tags = requests.get("http://127.0.0.1:11434/api/tags", timeout=10)
        if tags.ok:
            payload = tags.json() or {}
            installed = []
            for model in payload.get("models", []):
                name = str(model.get("name", "")).strip()
                if name:
                    installed.append(name)

            preferred = ["llama3.1", "llama3", "mistral", "phi3"]
            for pref in preferred:
                for name in installed:
                    if name == pref or name.startswith(pref + ":"):
                        model_candidates.append(name)
    except Exception as exc:
        print("Could not query Ollama model tags:", exc)

    model_candidates.extend(["llama3.1", "llama3", "mistral", "phi3"])
    seen = set()
    ordered_models = []
    for item in model_candidates:
        m = (item or "").strip()
        if not m or m in seen:
            continue
        seen.add(m)
        ordered_models.append(m)

    prompt = (
        "You are Future, a helpful personal AI assistant. "
        "Answer clearly and concisely.\n\n"
        f"User: {query}\nFuture:"
    )

    for model_name in ordered_models:
        try:
            response = requests.post(
                "http://127.0.0.1:11434/api/generate",
                json={
                    "model": model_name,
                    "prompt": prompt,
                    "stream": False,
                },
                timeout=60,
            )
            if response.ok:
                payload = response.json() or {}
                text = _clean_model_text(payload.get("response", ""))
                if text:
                    return text
        except Exception as exc:
            print(f"Ollama HTTP failed for {model_name}: {exc}")
            continue

    for model_name in ordered_models:
        try:
            result = subprocess.run(
                ["ollama", "run", model_name],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=60,
            )
            text = _clean_model_text(result.stdout or "")
            if text:
                return text
        except Exception as exc:
            print(f"Ollama fallback failed for {model_name}: {exc}")
            continue
    return None


def _response_length_profile(query: str) -> tuple[int, str]:
    lowered = (query or "").lower()

    brief_markers = [
        "brief",
        "short answer",
        "quick answer",
        "quick summary",
        "concise",
        "tldr",
        "tl;dr",
        "one sentence",
    ]
    depth_markers = [
        "in depth",
        "in-depth",
        "super in depth",
        "deep dive",
        "go deep",
        "more detail",
        "detailed",
        "thorough",
        "step by step",
        "full explanation",
        "long answer",
        "longer answer",
        "yap",
        "expand on",
        "explain like",
    ]

    asks_brief = any(marker in lowered for marker in brief_markers)
    asks_depth = any(marker in lowered for marker in depth_markers)

    if asks_brief and not asks_depth:
        return 280, "The user explicitly asked for a short answer. Keep it tight and compact."
    if asks_depth and not asks_brief:
        return 1600, "The user explicitly asked for depth. Provide a long, step-by-step, thorough answer."
    return 650, "Keep answers concise by default unless the user asks for greater depth."


def _model_candidates() -> List[str]:
    ordered = []
    for candidate in [PRIMARY_MODEL, BACKUP_MODEL, ANTHROPIC_MODEL, "gpt-5", "gpt-4.1", "claude-sonnet-4-5"]:
        model_name = (candidate or "").strip()
        if model_name and model_name not in ordered:
            ordered.append(model_name)
    return ordered


def _try_model_candidates(messages, max_tokens: int) -> Optional[str]:
    """Call each configured model in order and return the first successful cleaned reply."""
    for model_name in _model_candidates():
        try:
            if _is_anthropic_model(model_name):
                cleaned = _anthropic_reply(messages, model_name=model_name, max_tokens=max_tokens)
            elif _client:
                response = _client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    max_completion_tokens=max_tokens,
                )
                text = response.choices[0].message.content
                cleaned = _clean_model_text(text or "") if text else None
            else:
                cleaned = None

            if cleaned:
                return cleaned
        except Exception as exc:
            provider = "Anthropic" if _is_anthropic_model(model_name) else "OpenAI"
            print(f"{provider} error with model {model_name}: {exc}")
    return None


def generate_opening_greeting(client_time: Optional[str] = None) -> str:
    """Generate a one-off, time-aware opening line in Future's voice (not stored as a chat turn)."""
    personality = load_personality()
    fallback = "Hey, what are we working on?"
    messages = [
        {
            "role": "system",
            "content": (
                f"You are {personality['name']}, a personal AI assistant with traits "
                f"{personality['traits']} and tone {personality['tone']}. "
                f"{time_context(client_time)} "
                "Write a single short, natural opening line to greet the user as the chat first opens. "
                "Vary the wording each time, sound like yourself rather than a generic assistant, "
                "and only reference the time of day if it feels natural. "
                "Do not literally say 'what can I help you with' every time. One sentence only, no quotes."
            ),
        },
        {"role": "user", "content": "(system: generate the opening greeting now)"},
    ]
    try:
        cleaned = _try_model_candidates(messages, max_tokens=60)
        if cleaned:
            return cleaned.strip().strip('"')
    except Exception as exc:
        print(f"Greeting generation failed: {exc}")
    return fallback


def handle_query(query: str, recent_context=None, client_time: Optional[str] = None) -> str:
    """Route integration commands first; otherwise answer with cloud model if available."""
    query = (query or "").strip()
    if not query:
        return "I didn't catch that."

    local_reply = _handle_local_intents(query)
    if local_reply:
        return local_reply

    memory_reply = _handle_memory_intents(query)
    if memory_reply:
        return memory_reply

    memory = None
    messages = None
    try:
        memory, messages = _build_chat_messages(query, recent_context=recent_context, client_time=client_time)
    except Exception as exc:
        print(f"Could not load chat memory context: {exc}")
        memory = None
        messages = [
            {
                "role": "system",
                "content": (
                    "You are Future, a highly capable personal AI assistant. "
                    "Be concise, practical, and action-oriented."
                ),
            },
            {"role": "user", "content": query},
        ]

    max_tokens, style_directive = _response_length_profile(query)

    if messages and messages[0].get("role") == "system":
        system_content = str(messages[0].get("content", "")).strip()
        messages[0]["content"] = f"{system_content}\n\nCurrent response style: {style_directive}"

    cleaned = _try_model_candidates(messages, max_tokens)
    if cleaned:
        if memory is not None:
            remember(memory, query, cleaned)
            save_memory(memory)
        return cleaned

    if ALLOW_OLLAMA_FALLBACK:
        local_model_reply = _reply_with_local_model(query)
        if local_model_reply:
            return local_model_reply

        ollama_reply = _reply_with_ollama_fallback(query)
        if ollama_reply:
            return ollama_reply

    return _local_fallback_reply(query)
