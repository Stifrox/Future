"""FastAPI server for Future AI assistant - provides REST endpoints for chat, integrations, and system control."""
import os
import platform
import random
import json
import re
import base64
import secrets
import subprocess
import time
import urllib.parse
import webbrowser
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")
except Exception:
    pass

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

try:
    from tools.alpaca_trading import AutopilotPaperTrader
except Exception:
    AutopilotPaperTrader = None
from tools.anycubic import fetch_print_status, slice_and_send, slice_model, upload_gcode
from tools.memory import load_memory, remember, save_memory
from updater import self_update_execute_latest, self_update_plan
from tools.integrations import (
    complete_google_calendar_authorization,
    exchange_spotify_code,
    get_google_auth_url,
    get_spotify_auth_url,
    get_spotify_now_playing,
    handle_gmail_command,
    list_gmail_messages,
    list_google_calendar_events,
    run_spotify_callback_server,
    send_gmail_message,
    set_spotify_tokens,
    spotify_next,
    spotify_pause,
    spotify_play,
    spotify_previous,
)

try:
    import psutil
except Exception:
    psutil = None

app = FastAPI(title="Future API", version="1.0.0")

ACCESS_PASSWORD = os.getenv("FUTURE_ACCESS_PASSWORD", "4056")
ACCESS_COOKIE = "future_access"
MAX_ACCESS_ATTEMPTS = 3
ACCESS_LOCKOUT_SECONDS = 300
ACCESS_SESSION_SECONDS = 300
_access_failures: Dict[str, int] = {}
_access_lockouts: Dict[str, float] = {}


def _access_client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def require_access_password(request: Request, call_next):
    path = request.url.path
    is_public_api = path in {"/api/auth/status", "/api/auth/verify"}
    if path.startswith("/api/") and not is_public_api:
        if request.cookies.get(ACCESS_COOKIE) != "unlocked":
            return JSONResponse(status_code=401, content={"detail": "Future is locked"})
    return await call_next(request)

cors_origins = [origin.strip() for origin in os.getenv("FUTURE_CORS_ORIGINS", "*").split(",") if origin.strip()]
if cors_origins == ["*"]:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

trader = AutopilotPaperTrader() if AutopilotPaperTrader else None
CHAT_CONTEXT_MAX_LINES = int(os.getenv("FUTURE_CHAT_CONTEXT_LINES", "20"))
CHAT_CONTEXT_LINES = deque(maxlen=max(2, CHAT_CONTEXT_MAX_LINES))
AUTODESK_TOKEN_FILE = Path("data/autodesk_tokens.json")
AUTODESK_AUTH_URL = "https://developer.api.autodesk.com/authentication/v2/authorize"
AUTODESK_TOKEN_URL = "https://developer.api.autodesk.com/authentication/v2/token"
AUTODESK_USERINFO_URL = "https://api.userprofile.autodesk.com/userinfo"
AUTODESK_PROJECTS_API = "https://developer.api.autodesk.com/project/v1"
AUTODESK_DATA_API = "https://developer.api.autodesk.com/data/v1"
_AUTODESK_OAUTH_STATE: Optional[str] = None


class SpotifyControlRequest(BaseModel):
    action: str


class ChatMessageRequest(BaseModel):
    message: str


class GmailSendRequest(BaseModel):
    to: str
    subject: str = "Future message"
    body: str = ""


class ImageGenerateRequest(BaseModel):
    prompt: str
    size: str = "1024x1024"
    model: str = "gpt-image-1"


class ImageAnalyzeRequest(BaseModel):
    image_data_url: str
    question: str = "What do you see in this image?"
    model: str = "gpt-4.1-mini"


class ImageEditRequest(BaseModel):
    image_data_url: str
    prompt: str
    size: str = "1024x1024"
    model: str = "gpt-image-1"


class TtsRequest(BaseModel):
    text: str
    voice_gender: str = "male"


class AnycubicSliceRequest(BaseModel):
    input_path: str
    output_path: str = ""
    profile_path: str = ""
    start_print: bool = False


class AnycubicUploadRequest(BaseModel):
    gcode_path: str
    start_print: bool = False


class FileWriteRequest(BaseModel):
    path: str
    content: str
    overwrite: bool = False


class FusionOpenRequest(BaseModel):
    file_name: str
    open_in_browser: bool = True


class SelfUpdatePlanRequest(BaseModel):
    instruction: str
    target_files: List[str] = []
    scope: str = "auto"


class AccessPasswordRequest(BaseModel):
    password: str


def _safe_float(value, default=0.0):
    """Safely convert value to float with fallback default."""
    try:
        return float(value)
    except Exception:
        return default


def _openai_client_from_env():
    api_key = (os.getenv("OPENAI_API_KEY", "") or os.getenv("FUTURE_OPENAI_API_KEY", "")).strip()
    if not api_key or OpenAI is None:
        return None
    try:
        return OpenAI(api_key=api_key)
    except Exception:
        return None


def _image_output_dir() -> Path:
    path = Path("generated") / "images"
    path.mkdir(parents=True, exist_ok=True)
    return path

def _chat_reply(message: str, recent_context=None) -> str:
    """Resolve chat handler lazily so optional config mismatches do not break API startup."""
    try:
        from webtools import handle_query as _handle_query  # Local import by design.
        return _handle_query(message, recent_context=recent_context)
    except Exception:
        return "I can help with schedule, inbox, prints, and files once the model config is ready."

def _synthesize_elevenlabs_audio(text: str, voice_gender: str = "male") -> bytes:
    api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    voice_id_male = os.getenv("ELEVENLABS_VOICE_ID", "").strip()
    voice_id_female = os.getenv("ELEVENLABS_VOICE_ID_FEMALE", "").strip()
    voice_id = voice_id_female if voice_gender.lower() == "female" and voice_id_female else voice_id_male
    model_id = os.getenv("ELEVENLABS_MODEL_ID", "eleven_turbo_v2_5").strip() or "eleven_turbo_v2_5"

    if not api_key or not voice_id:
        raise RuntimeError("ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID must be configured")

    response = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        headers={
            "xi-api-key": api_key,
            "accept": "audio/mpeg",
            "content-type": "application/json",
        },
        json={
            "text": text,
            "model_id": model_id,
            "output_format": "mp3_44100_128",
        },
        timeout=45,
    )
    if not response.ok:
        raise RuntimeError(f"ElevenLabs API error ({response.status_code}): {response.text[:400]}")

    if not response.content:
        raise RuntimeError("ElevenLabs returned empty audio data")
    return response.content


def _tts_intro_preview(text: str, max_chars: int = 320, max_lines: int = 3, max_sentences: int = 3) -> str:
    cleaned = str(text or "").replace("\r", "").strip()
    if not cleaned:
        return ""

    def _speech_clean_line(value: str) -> str:
        line = value.strip()
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"^[-*]\s+", "", line)
        line = re.sub(r"^\d+[.)]\s+", "", line)
        line = re.sub(r"\*\*(.*?)\*\*", r"\1", line)
        line = re.sub(r"`([^`]+)`", r"\1", line)
        return line.strip()

    non_empty_lines = [_speech_clean_line(line) for line in cleaned.split("\n") if line.strip()]
    non_empty_lines = [line for line in non_empty_lines if line]

    # Keep short replies untouched so normal conversations still sound complete.
    if len(cleaned) <= max_chars and len(non_empty_lines) <= max_lines:
        return cleaned[:1200]

    intro_lines = non_empty_lines[:max_lines]
    intro_text = " ".join(intro_lines).strip()

    """Normalize chat reply text, handle fallback messages and branding replacements."""
    if len(intro_text) < 60:
        sentences = [
            segment.strip()
            for segment in re.split(r"(?<=[.!?])\s+", _speech_clean_line(cleaned))
            if segment.strip()
        ]
        intro_text = " ".join(sentences[:max_sentences]).strip()
    """Convert byte count to human-readable size string (B, KB, MB, GB, TB)."""

    """Normalize chat reply text, handle fallback messages and branding replacements."""
    if len(intro_text) > max_chars:
        intro_text = intro_text[:max_chars].rstrip(" ,;:-") + "..."

    return intro_text[:1200]


def _normalize_chat_reply(reply: str) -> str:
    text = (reply or "").strip()
    if not text:
        return "Future is online, but I could not generate a reply for that message."
    if text.startswith("[Local fallback]"):
        return "Future is running without the cloud model right now. I can still help with local integrations while model access is restored."
    return text.replace("Atlas", "Future").replace("ATLAS", "FUTURE")


def _human_size(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(max(0, num_bytes))
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return "0 B"


def _format_relative_modified(timestamp: float) -> str:
    dt = datetime.fromtimestamp(timestamp)
    """Recursively discover Fusion 360 project files in configured root directory."""
    now = datetime.now()
    if dt.date() == now.date():
        return dt.strftime("%I:%M %p").lstrip("0")
    if (now.date() - dt.date()).days == 1:
        return "Yesterday"
    return dt.strftime("%b %d")


def _discover_fusion_projects(max_results: int = 12) -> List[Dict[str, str]]:
    root = Path(os.getenv("FUTURE_FUSION_ROOT", str(Path.cwd()))).expanduser().resolve()
    suffixes = {".f3d", ".f3z", ".step", ".stp", ".iges", ".igs"}
    skip_dirs = {"venv", "venv310", ".git", "node_modules", "__pycache__"}
    discovered: List[Dict[str, object]] = []

    for path in root.rglob("*"):
        if any(part in skip_dirs for part in path.parts):
            continue
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        try:
            stat = path.stat()
            discovered.append(
                {
                    "name": path.stem,
                    "modified": _format_relative_modified(stat.st_mtime),
                    "path": str(path),
                    "_mtime": stat.st_mtime,
                }
            )
        except Exception:
            continue

    discovered.sort(key=lambda row: row.get("_mtime", 0), reverse=True)
    return [
        {"name": str(row["name"]), "modified": str(row["modified"]), "path": str(row["path"])}
        for row in discovered[:max_results]
    ]


def _autodesk_credentials() -> Tuple[str, str, str, str]:
    client_id = os.getenv("AUTODESK_CLIENT_ID", "").strip()
    client_secret = os.getenv("AUTODESK_CLIENT_SECRET", "").strip()
    redirect_uri = os.getenv("AUTODESK_REDIRECT_URI", "http://localhost:8000/callback/autodesk").strip()
    scopes = os.getenv("AUTODESK_SCOPES", "data:read").strip() or "data:read"
    return client_id, client_secret, redirect_uri, scopes


def _load_autodesk_tokens() -> Dict[str, object]:
    if not AUTODESK_TOKEN_FILE.exists():
        return {}
    try:
        payload = json.loads(AUTODESK_TOKEN_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _save_autodesk_tokens(payload: Dict[str, object]) -> None:
    AUTODESK_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    merged = _load_autodesk_tokens()
    merged.update(payload)
    merged["updated_at"] = datetime.utcnow().isoformat() + "Z"
    AUTODESK_TOKEN_FILE.write_text(json.dumps(merged, indent=2), encoding="utf-8")


def _autodesk_token_active(token_payload: Dict[str, object]) -> bool:
    token = str(token_payload.get("access_token", "")).strip()
    expires_at = float(token_payload.get("expires_at", 0) or 0)
    if not token or not expires_at:
        return False
    return datetime.utcnow().timestamp() < max(0.0, expires_at - 60)


def _autodesk_refresh_token() -> Optional[str]:
    client_id, client_secret, _, _ = _autodesk_credentials()
    token_payload = _load_autodesk_tokens()
    refresh_token = str(token_payload.get("refresh_token", "")).strip()
    if not client_id or not client_secret or not refresh_token:
        return None

    response = requests.post(
        AUTODESK_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    if not response.ok:
        return None

    payload = response.json() or {}
    access_token = str(payload.get("access_token", "")).strip()
    if not access_token:
        return None

    expires_in = int(payload.get("expires_in", 0) or 0)
    expires_at = datetime.utcnow().timestamp() + max(0, expires_in)
    _save_autodesk_tokens(
        {
            "access_token": access_token,
            "refresh_token": str(payload.get("refresh_token", refresh_token) or refresh_token),
            "expires_in": expires_in,
            "expires_at": expires_at,
            "token_type": str(payload.get("token_type", "Bearer")),
        }
    )
    return access_token


def _autodesk_access_token() -> str:
    token_payload = _load_autodesk_tokens()
    if _autodesk_token_active(token_payload):
        return str(token_payload.get("access_token", "")).strip()

    refreshed = _autodesk_refresh_token()
    if refreshed:
        return refreshed

    raise HTTPException(status_code=401, detail="Fusion OAuth token missing or expired. Reconnect Fusion.")


def _autodesk_request(url: str, token: str, params: Optional[Dict[str, str]] = None) -> Dict[str, object]:
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=40,
    )
    if not response.ok:
        detail = response.text.strip()[:600]
        raise HTTPException(status_code=response.status_code, detail=f"Fusion API error: {detail}")
    payload = response.json() if response.text else {}
    return payload if isinstance(payload, dict) else {}


def _autodesk_fetch_projects(token: str, max_hubs: int = 5, max_projects_per_hub: int = 25) -> List[Dict[str, str]]:
    hubs_payload = _autodesk_request(f"{AUTODESK_PROJECTS_API}/hubs", token)
    hubs = hubs_payload.get("data", []) if isinstance(hubs_payload.get("data", []), list) else []

    projects: List[Dict[str, str]] = []
    for hub in hubs[: max(1, max_hubs)]:
        if not isinstance(hub, dict):
            continue
        hub_id = str(hub.get("id", "")).strip()
        hub_name = str((hub.get("attributes") or {}).get("name", "")).strip() or "Hub"
        if not hub_id:
            continue

        project_url = f"{AUTODESK_PROJECTS_API}/hubs/{urllib.parse.quote(hub_id, safe='')}/projects"
        project_payload = _autodesk_request(project_url, token)
        rows = project_payload.get("data", []) if isinstance(project_payload.get("data", []), list) else []

        for project in rows[: max(1, max_projects_per_hub)]:
            if not isinstance(project, dict):
                continue
            project_id = str(project.get("id", "")).strip()
            attrs = project.get("attributes") or {}
            project_name = str(attrs.get("name", "")).strip() or "Untitled"
            updated_at = str(attrs.get("lastModifiedTime", ""))
            if not project_id:
                continue
            projects.append(
                {
                    "hub_id": hub_id,
                    "hub_name": hub_name,
                    "project_id": project_id,
                    "project_name": project_name,
                    "updated_at": updated_at,
                }
            )

    return projects


def _autodesk_search_project_items(token: str, hub_id: str, project_id: str, query: str) -> List[Dict[str, str]]:
    top_folder_url = (
        f"{AUTODESK_PROJECTS_API}/hubs/{urllib.parse.quote(hub_id, safe='')}"
        f"/projects/{urllib.parse.quote(project_id, safe='')}/topFolders"
    )
    top_payload = _autodesk_request(top_folder_url, token)
    top_folders = top_payload.get("data", []) if isinstance(top_payload.get("data", []), list) else []

    matches: List[Dict[str, str]] = []
    lowered = query.lower().strip()
    for folder in top_folders[:8]:
        folder_id = str((folder or {}).get("id", "")).strip()
        if not folder_id:
            continue

        contents_url = (
            f"{AUTODESK_DATA_API}/projects/{urllib.parse.quote(project_id, safe='')}"
            f"/folders/{urllib.parse.quote(folder_id, safe='')}/contents"
        )
        contents_payload = _autodesk_request(contents_url, token)
        contents = contents_payload.get("data", []) if isinstance(contents_payload.get("data", []), list) else []

        for item in contents:
            if not isinstance(item, dict):
                continue
            attrs = item.get("attributes") or {}
            name = str(attrs.get("displayName", attrs.get("name", ""))).strip()
            item_id = str(item.get("id", "")).strip()
            if not name or not item_id:
                continue
            if lowered not in name.lower():
                continue

            links = item.get("links") or {}
            web_view = links.get("webView") or {}
            web_url = str(web_view.get("href", "")).strip()
            version_urn = ""
            relationships = item.get("relationships") or {}
            tip = relationships.get("tip") or {}
            tip_data = tip.get("data") or {}
            version_urn = str(tip_data.get("id", "")).strip()

            matches.append(
                {
                    "name": name,
                    "item_id": item_id,
                    "project_id": project_id,
                    "hub_id": hub_id,
                    "web_url": web_url,
                    "version_urn": version_urn,
                }
            )

    return matches


def _demo_stocks() -> List[Dict[str, object]]:
    return [
        {"ticker": "AAPL", "name": "Apple Inc.", "price": 175.04, "change_pct": 1.32, "spark": [1, 2, 1.5, 2.2, 2, 2.6, 3]},
        {"ticker": "TSLA", "name": "Tesla Inc.", "price": 248.21, "change_pct": 2.68, "spark": [3, 2.4, 2.8, 3.2, 3, 3.6, 4]},
        {"ticker": "NVDA", "name": "NVIDIA Corp.", "price": 950.02, "change_pct": 1.21, "spark": [2, 2.3, 2.1, 2.6, 2.9, 2.7, 3.1]},
        {"ticker": "MSFT", "name": "Microsoft", "price": 405.18, "change_pct": -0.42, "spark": [3, 2.8, 2.9, 2.6, 2.4, 2.5, 2.2]},
    ]


def _files_root() -> Path:
    configured = os.getenv("FUTURE_FILES_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.cwd().resolve()


def _resolve_directory(user_path: str) -> Path:
    root = _files_root()
    candidate = (root / user_path.lstrip("/").strip()).resolve()
    if root not in candidate.parents and candidate != root:
        raise HTTPException(status_code=400, detail="Path is outside allowed root")
    if not candidate.exists() or not candidate.is_dir():
        raise HTTPException(status_code=404, detail="Directory not found")
    return candidate


def _resolve_file(user_path: str) -> Path:
    root = _files_root()
    normalized = (user_path or "").strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="File path is required")

    candidate = (root / normalized.lstrip("/")).resolve()
    if root not in candidate.parents and candidate != root:
        raise HTTPException(status_code=400, detail="Path is outside allowed root")
    if candidate == root:
        raise HTTPException(status_code=400, detail="File path must not be the workspace root")
    return candidate


def _downloads_directory() -> Path:
    configured = os.getenv("FUTURE_DOWNLOADS_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / "Downloads"


_DASHBOARD_HTML_PATH = Path(__file__).parent / "dashboard.html"
if not _DASHBOARD_HTML_PATH.exists():
    _DASHBOARD_HTML_PATH = Path(r"C:\Users\tallm\Downloads\atlas-dashboard.html")
_APP_ROOT = Path(__file__).parent
_PWA_MANIFEST_PATH = _APP_ROOT / "manifest.json"
_PWA_SERVICE_WORKER_PATH = _APP_ROOT / "sw.js"
_PWA_ICON_DIRECTORY = _APP_ROOT / "icons"


@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
def serve_dashboard() -> HTMLResponse:
    if _DASHBOARD_HTML_PATH.exists():
        content = _DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
        # Patch the default API base URL so it points to this server automatically
        content = content.replace(
            "API_BASE: 'http://localhost:8000'",
            "API_BASE: 'http://localhost:8000'",
        )
        return HTMLResponse(content=content)
    return HTMLResponse(
        content="<h1>Dashboard HTML not found</h1><p>Place atlas-dashboard.html in Downloads or set the path in api_server.py.</p>",
        status_code=404,
    )


@app.get("/manifest.json")
def serve_manifest() -> FileResponse:
    return FileResponse(_PWA_MANIFEST_PATH, media_type="application/manifest+json")


@app.get("/sw.js")
def serve_service_worker() -> FileResponse:
    return FileResponse(_PWA_SERVICE_WORKER_PATH, media_type="application/javascript")


@app.get("/apple-touch-icon.png")
def serve_apple_touch_icon() -> FileResponse:
    return FileResponse(_PWA_ICON_DIRECTORY / "future-icon-ios-180.png", media_type="image/png")


@app.get("/icons/{icon_name}")
def serve_icon(icon_name: str) -> FileResponse:
    icon_path = (_PWA_ICON_DIRECTORY / icon_name).resolve()
    if _PWA_ICON_DIRECTORY.resolve() not in icon_path.parents or not icon_path.is_file():
        raise HTTPException(status_code=404, detail="Icon not found")
    media_type = "image/png" if icon_path.suffix.lower() == ".png" else "image/svg+xml"
    return FileResponse(icon_path, media_type=media_type)


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/api/auth/status")
def access_status(request: Request) -> Response:
    unlocked = request.cookies.get(ACCESS_COOKIE) == "unlocked"
    response = JSONResponse(content={"unlocked": unlocked})
    if unlocked:
        response.set_cookie(
            ACCESS_COOKIE,
            "unlocked",
            max_age=ACCESS_SESSION_SECONDS,
            httponly=True,
            samesite="lax",
        )
    return response


@app.post("/api/auth/verify")
def verify_access_password(payload: AccessPasswordRequest, request: Request) -> Response:
    client_key = _access_client_key(request)
    now = time.time()
    locked_until = _access_lockouts.get(client_key, 0)
    if locked_until > now:
        retry_after = max(1, int(locked_until - now))
        return JSONResponse(
            status_code=429,
            content={"detail": f"Too many failed attempts. Try again in {retry_after} seconds."},
            headers={"Retry-After": str(retry_after)},
        )

    if secrets.compare_digest(payload.password, ACCESS_PASSWORD):
        _access_failures.pop(client_key, None)
        _access_lockouts.pop(client_key, None)
        response = JSONResponse(content={"unlocked": True})
        response.set_cookie(
            ACCESS_COOKIE,
            "unlocked",
            max_age=ACCESS_SESSION_SECONDS,
            httponly=True,
            samesite="lax",
        )
        return response

    failures = _access_failures.get(client_key, 0) + 1
    _access_failures[client_key] = failures
    if failures >= MAX_ACCESS_ATTEMPTS:
        _access_lockouts[client_key] = now + ACCESS_LOCKOUT_SECONDS
        _access_failures.pop(client_key, None)
        return JSONResponse(
            status_code=429,
            content={"detail": "Three failed attempts. Future is locked for 5 minutes."},
            headers={"Retry-After": str(ACCESS_LOCKOUT_SECONDS)},
        )
    return JSONResponse(
        status_code=401,
        content={"detail": f"Incorrect password. {MAX_ACCESS_ATTEMPTS - failures} attempts remaining."},
    )


def _gpu_utilization() -> float:
    configured = os.getenv("FUTURE_GPU_UTIL", "").strip()
    if configured:
        return max(0.0, min(100.0, _safe_float(configured)))

    if platform.system() != "Windows":
        return 0.0

    command = (
        "Get-Counter '\\GPU Engine(*)\\Utilization Percentage' "
        "-ErrorAction SilentlyContinue | "
        "Select-Object -ExpandProperty CounterSamples | "
        "Select-Object -ExpandProperty CookedValue"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        values = []
        for line in result.stdout.splitlines():
            try:
                values.append(float(line.strip().replace(",", ".")))
            except ValueError:
                continue
        return max(0.0, min(100.0, max(values, default=0.0)))
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0.0


@app.get("/api/system/stats")
def get_system_stats() -> Dict[str, float]:
    if psutil is None:
        return {
            "cpu": round(30 + random.uniform(-8, 8), 2),
            "ram": round(45 + random.uniform(-6, 6), 2),
            "gpu": round(_gpu_utilization(), 2),
            "net_mbps": round(8 + random.uniform(-3, 3), 2),
        }

    net1 = psutil.net_io_counters()
    cpu = psutil.cpu_percent(interval=0.2)
    ram = psutil.virtual_memory().percent
    net2 = psutil.net_io_counters()
    net_mbps = max(0.0, (net2.bytes_recv - net1.bytes_recv) * 8 / 1_000_000)

    return {
        "cpu": round(cpu, 2),
        "ram": round(ram, 2),
        "gpu": round(_gpu_utilization(), 2),
        "net_mbps": round(net_mbps, 2),
    }


@app.get("/api/spotify/now-playing")
def spotify_now_playing() -> Dict[str, object]:
    try:
        return get_spotify_now_playing()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Spotify unavailable: {exc}") from exc


@app.post("/api/spotify/control")
def spotify_control(payload: SpotifyControlRequest) -> Dict[str, str]:
    action = payload.action.strip().lower()
    if action == "play":
        spotify_play()
    elif action == "pause":
        spotify_pause()
    elif action == "next":
        spotify_next()
    elif action == "prev":
        spotify_previous()
    else:
        raise HTTPException(status_code=400, detail="Action must be one of play, pause, next, prev")
    return {"status": "ok", "action": action}


@app.get("/api/calendar/events")
def calendar_events(range: str = Query(default="today")) -> List[Dict[str, str]]:
    try:
        max_results = 50 if (range or "").strip().lower() in {"week", "month", "next7", "7d"} else 8
        return list_google_calendar_events(range_name=range, max_results=max_results)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Calendar unavailable: {exc}") from exc


@app.get("/api/gmail/inbox")
def gmail_inbox(limit: int = Query(default=5, ge=1, le=20)) -> List[Dict[str, object]]:
    try:
        rows = list_gmail_messages(max_results=limit, query="in:inbox")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Gmail unavailable: {exc}") from exc

    normalized = []
    for row in rows:
        normalized.append(
            {
                "from": row.get("from", "unknown"),
                "subject": row.get("subject", "(no subject)"),
                "time": row.get("date", "now")[:16] if row.get("date") else "now",
                "unread": True,
            }
        )
    return normalized


@app.post("/api/gmail/send")
def gmail_send(payload: GmailSendRequest) -> Dict[str, str]:
    to_value = (payload.to or "").strip()
    subject_value = (payload.subject or "Future message").strip() or "Future message"
    body_value = (payload.body or "").strip()

    if not to_value:
        raise HTTPException(status_code=400, detail="Recipient is required")
    if not body_value:
        raise HTTPException(status_code=400, detail="Email body is required")

    try:
        if "@" in to_value:
            send_gmail_message(to_value, subject_value, body_value)
            return {"status": "sent", "to": to_value, "subject": subject_value}

        # Fallback for contact aliases handled by existing integration logic.
        draft_reply = handle_gmail_command(
            f"send email to {to_value} subject {subject_value} message {body_value}"
        )
        if "Should I send it now?" in draft_reply:
            confirm_reply = handle_gmail_command("yes")
            if "Gmail sent" in confirm_reply:
                return {"status": "sent", "to": to_value, "subject": subject_value}
            raise HTTPException(status_code=400, detail=confirm_reply)

        if "Gmail sent" in draft_reply:
            return {"status": "sent", "to": to_value, "subject": subject_value}

        raise HTTPException(status_code=400, detail=draft_reply)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Gmail unavailable: {exc}") from exc


@app.post("/api/images/generate")
def image_generate(payload: ImageGenerateRequest) -> Dict[str, str]:
    prompt = (payload.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")

    client = _openai_client_from_env()
    if not client:
        raise HTTPException(status_code=503, detail="OpenAI image generation is not configured")

    try:
        result = client.images.generate(
            model=(payload.model or "gpt-image-1").strip() or "gpt-image-1",
            prompt=prompt,
            size=(payload.size or "1024x1024").strip() or "1024x1024",
        )
        data = getattr(result, "data", None) or []
        if not data:
            raise RuntimeError("No image data returned")

        b64 = getattr(data[0], "b64_json", None)
        if not b64:
            raise RuntimeError("Image response did not include base64 payload")

        raw = base64.b64decode(b64)
        image_id = uuid.uuid4().hex[:12]
        out_path = _image_output_dir() / f"generated_{image_id}.png"
        out_path.write_bytes(raw)

        return {
            "image_data_url": f"data:image/png;base64,{b64}",
            "file": str(out_path).replace("\\", "/"),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Image generation failed: {exc}") from exc


@app.post("/api/images/analyze")
def image_analyze(payload: ImageAnalyzeRequest) -> Dict[str, str]:
    image_data_url = (payload.image_data_url or "").strip()
    if not image_data_url:
        raise HTTPException(status_code=400, detail="image_data_url is required")
    if not image_data_url.startswith("data:image/"):
        raise HTTPException(status_code=400, detail="image_data_url must be a data URL")

    question = (payload.question or "What do you see in this image?").strip() or "What do you see in this image?"
    client = _openai_client_from_env()
    if not client:
        raise HTTPException(status_code=503, detail="OpenAI vision is not configured")

    model_name = (payload.model or "gpt-4.1-mini").strip() or "gpt-4.1-mini"
    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {"type": "image_url", "image_url": {"url": image_data_url}},
                    ],
                }
            ],
            max_completion_tokens=500,
        )
        text = ""
        if getattr(response, "choices", None):
            text = (response.choices[0].message.content or "").strip()
        if not text:
            text = "I could not extract details from that image."

        # Save a concise record of the analyzed image content to long-term memory.
        try:
            memory = load_memory()
            remember(
                memory,
                f"image analysis request: {question[:280]}",
                f"Image analysis result: {text[:1600]}",
            )
            save_memory(memory)
        except Exception as memory_exc:
            # Do not fail image analysis if memory persistence has an issue.
            print(f"Could not persist image analysis memory: {memory_exc}")

        return {"analysis": text}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Image analysis failed: {exc}") from exc


@app.post("/api/images/edit")
def image_edit(payload: ImageEditRequest) -> Dict[str, str]:
    image_data_url = (payload.image_data_url or "").strip()
    prompt = (payload.prompt or "").strip()
    if not image_data_url:
        raise HTTPException(status_code=400, detail="image_data_url is required")
    if not image_data_url.startswith("data:image/"):
        raise HTTPException(status_code=400, detail="image_data_url must be a data URL")
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")

    client = _openai_client_from_env()
    if not client:
        raise HTTPException(status_code=503, detail="OpenAI image edit is not configured")

    try:
        header, b64_data = image_data_url.split(",", 1)
        _ = header
        raw = base64.b64decode(b64_data)

        image_id = uuid.uuid4().hex[:12]
        source_path = _image_output_dir() / f"edit_source_{image_id}.png"
        source_path.write_bytes(raw)

        with source_path.open("rb") as image_file:
            result = client.images.edit(
                model=(payload.model or "gpt-image-1").strip() or "gpt-image-1",
                image=image_file,
                prompt=prompt,
                size=(payload.size or "1024x1024").strip() or "1024x1024",
            )

        data = getattr(result, "data", None) or []
        if not data:
            raise RuntimeError("No edited image returned")

        edited_b64 = getattr(data[0], "b64_json", None)
        if not edited_b64:
            raise RuntimeError("Edited image response did not include base64 payload")

        edited_raw = base64.b64decode(edited_b64)
        out_path = _image_output_dir() / f"edited_{image_id}.png"
        out_path.write_bytes(edited_raw)

        return {
            "image_data_url": f"data:image/png;base64,{edited_b64}",
            "file": str(out_path).replace("\\", "/"),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Image edit failed: {exc}") from exc


@app.get("/api/stocks/watchlist")
def stocks_watchlist() -> List[Dict[str, object]]:
    if trader is None:
        raise HTTPException(status_code=503, detail="Trading integration is unavailable on this installation")

    watchlist = [symbol.strip() for symbol in os.getenv("FUTURE_STOCK_WATCHLIST", "AAPL,TSLA,NVDA,MSFT").split(",") if symbol.strip()]
    if not watchlist:
        raise HTTPException(status_code=503, detail="Stock watchlist is not configured")

    try:
        prices = trader._fetch_live_prices(watchlist)
        if not prices:
            raise HTTPException(status_code=503, detail="Live stock prices are unavailable")

        results = []
        for symbol in watchlist:
            price = prices.get(symbol)
            if price is None:
                continue
            results.append(
                {
                    "ticker": symbol,
                    "name": symbol,
                    "price": round(float(price), 2),
                    "change_pct": 0.0,
                    "spark": [round(1 + random.uniform(-0.4, 0.8) + i * 0.15, 2) for i in range(7)],
                }
            )
        if not results:
            raise HTTPException(status_code=503, detail="No live prices returned for watchlist symbols")
        return results
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Live stock feed unavailable: {exc}") from exc


@app.get("/api/fusion360/recent-files")
def fusion_recent_files() -> List[Dict[str, str]]:
    try:
        token = _autodesk_access_token()
        projects = _autodesk_fetch_projects(token, max_hubs=3, max_projects_per_hub=12)
        if projects:
            return [
                {
                    "name": row.get("project_name", "Untitled"),
                    "modified": row.get("updated_at", ""),
                    "path": row.get("project_id", ""),
                }
                for row in projects
            ]
    except Exception:
        pass

    state_file = Path("data/fusion_recent_files.json")
    if state_file.exists():
        try:
            payload = json.loads(state_file.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                normalized = []
                for row in payload:
                    if not isinstance(row, dict):
                        continue
                    normalized.append(
                        {
                            "name": str(row.get("name", "Untitled")),
                            "modified": str(row.get("modified", "Unknown")),
                            "path": str(row.get("path", "")),
                        }
                    )
                if normalized:
                    return normalized
        except Exception:
            pass

    return _discover_fusion_projects()


@app.post("/api/connect/fusion360")
def connect_fusion360() -> Dict[str, str]:
    global _AUTODESK_OAUTH_STATE
    client_id, client_secret, redirect_uri, scopes = _autodesk_credentials()
    if not client_id or not client_secret:
        raise HTTPException(status_code=400, detail="AUTODESK_CLIENT_ID and AUTODESK_CLIENT_SECRET are required")

    _AUTODESK_OAUTH_STATE = secrets.token_urlsafe(24)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scopes,
        "state": _AUTODESK_OAUTH_STATE,
    }
    auth_url = f"{AUTODESK_AUTH_URL}?{urllib.parse.urlencode(params)}"
    return {"auth_url": auth_url, "state": _AUTODESK_OAUTH_STATE}


@app.get("/oauth/autodesk/callback", response_class=HTMLResponse)
@app.get("/callback/autodesk", response_class=HTMLResponse)
def autodesk_oauth_callback(code: str = "", state: str = "", error: str = "") -> HTMLResponse:
    global _AUTODESK_OAUTH_STATE

    if error:
        return HTMLResponse(f"<h1>Fusion authorization failed</h1><p>{error}</p>", status_code=400)
    if not code:
        return HTMLResponse("<h1>Fusion authorization failed</h1><p>Missing code</p>", status_code=400)
    if _AUTODESK_OAUTH_STATE and state and state != _AUTODESK_OAUTH_STATE:
        return HTMLResponse("<h1>Fusion authorization failed</h1><p>OAuth state mismatch</p>", status_code=400)

    client_id, client_secret, redirect_uri, _ = _autodesk_credentials()
    if not client_id or not client_secret:
        return HTMLResponse("<h1>Fusion authorization failed</h1><p>Missing AUTODESK credentials in environment.</p>", status_code=500)

    try:
        token_response = requests.post(
            AUTODESK_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=30,
        )
        if not token_response.ok:
            detail = token_response.text.strip()[:700]
            return HTMLResponse(f"<h1>Fusion authorization failed</h1><p>{detail}</p>", status_code=500)

        payload = token_response.json() or {}
        access_token = str(payload.get("access_token", "")).strip()
        if not access_token:
            return HTMLResponse("<h1>Fusion authorization failed</h1><p>No access token returned.</p>", status_code=500)

        expires_in = int(payload.get("expires_in", 0) or 0)
        expires_at = datetime.utcnow().timestamp() + max(0, expires_in)
        _save_autodesk_tokens(
            {
                "access_token": access_token,
                "refresh_token": str(payload.get("refresh_token", "")),
                "expires_in": expires_in,
                "expires_at": expires_at,
                "token_type": str(payload.get("token_type", "Bearer")),
                "scope": str(payload.get("scope", "")),
            }
        )
        _AUTODESK_OAUTH_STATE = None
    except Exception as exc:
        return HTMLResponse(f"<h1>Fusion authorization failed</h1><p>{exc}</p>", status_code=500)

    return HTMLResponse(
        "<html><body style='font-family:sans-serif;background:#0a0b0c;color:#e9e6e0;display:flex;"
        "align-items:center;justify-content:center;height:100vh;margin:0'>"
        "<div style='text-align:center'><h1 style='color:#6fa677'>Fusion Connected</h1>"
        "<p>Autodesk OAuth is complete. Future can now read your Fusion projects.</p></div></body></html>"
    )


@app.get("/api/fusion360/oauth/status")
def fusion_oauth_status() -> Dict[str, object]:
    token_payload = _load_autodesk_tokens()
    connected = _autodesk_token_active(token_payload)

    profile = {}
    if connected:
        try:
            token = str(token_payload.get("access_token", "")).strip()
            profile = _autodesk_request(AUTODESK_USERINFO_URL, token)
        except Exception:
            profile = {}

    return {
        "connected": connected,
        "has_refresh_token": bool(str(token_payload.get("refresh_token", "")).strip()),
        "expires_at": token_payload.get("expires_at", 0),
        "scopes": str(token_payload.get("scope", "")),
        "user": {
            "name": str(profile.get("name", "")),
            "email": str(profile.get("emailId", "")),
            "user_id": str(profile.get("userId", "")),
        },
    }


@app.get("/api/fusion360/projects")
def fusion_projects(limit: int = Query(default=30, ge=1, le=200)) -> List[Dict[str, str]]:
    token = _autodesk_access_token()
    projects = _autodesk_fetch_projects(token, max_hubs=8, max_projects_per_hub=50)
    return projects[:limit]


@app.get("/api/fusion360/files/search")
def fusion_files_search(query: str = Query(default="", min_length=2), limit: int = Query(default=25, ge=1, le=100)) -> List[Dict[str, str]]:
    token = _autodesk_access_token()
    projects = _autodesk_fetch_projects(token, max_hubs=4, max_projects_per_hub=20)

    matches: List[Dict[str, str]] = []
    for project in projects:
        project_hits = _autodesk_search_project_items(
            token,
            hub_id=project.get("hub_id", ""),
            project_id=project.get("project_id", ""),
            query=query,
        )
        for hit in project_hits:
            hit["project_name"] = project.get("project_name", "")
            hit["hub_name"] = project.get("hub_name", "")
            matches.append(hit)
            if len(matches) >= limit:
                return matches
    return matches


@app.post("/api/fusion360/open")
def fusion_open_file(payload: FusionOpenRequest) -> Dict[str, object]:
    token = _autodesk_access_token()
    projects = _autodesk_fetch_projects(token, max_hubs=4, max_projects_per_hub=20)

    for project in projects:
        hits = _autodesk_search_project_items(
            token,
            hub_id=project.get("hub_id", ""),
            project_id=project.get("project_id", ""),
            query=payload.file_name,
        )
        if not hits:
            continue

        best = hits[0]
        opened = False
        url = best.get("web_url", "")
        if payload.open_in_browser and url:
            try:
                opened = bool(webbrowser.open(url))
            except Exception:
                opened = False

        return {
            "found": True,
            "opened": opened,
            "file": best,
            "project_name": project.get("project_name", ""),
            "hub_name": project.get("hub_name", ""),
            "next_step": "Open web_url in browser if Fusion did not auto-open.",
        }

    return {
        "found": False,
        "opened": False,
        "file": {},
        "next_step": "Try /api/fusion360/files/search with a shorter query.",
    }


@app.get("/api/anycubic/status")
def anycubic_status() -> Dict[str, object]:
    try:
        return fetch_print_status()
    except Exception:
        pass

    status_file = Path("data/anycubic_status.json")
    if status_file.exists():
        try:
            payload = json.loads(status_file.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass

    return {
        "printing": False,
        "file": "Printer empty",
        "layer": 0,
        "layer_total": 0,
        "progress_pct": 0,
        "eta": "--",
        "nozzle_temp": 0,
    }


@app.post("/api/anycubic/slice")
def anycubic_slice(payload: AnycubicSliceRequest) -> Dict[str, object]:
    try:
        result = slice_model(
            input_path=payload.input_path,
            output_path=payload.output_path or None,
            profile_path=payload.profile_path or None,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return result


@app.post("/api/anycubic/upload")
def anycubic_upload(payload: AnycubicUploadRequest) -> Dict[str, object]:
    try:
        return upload_gcode(payload.gcode_path, start_print=payload.start_print)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/anycubic/slice-and-send")
def anycubic_slice_and_send(payload: AnycubicSliceRequest) -> Dict[str, object]:
    try:
        return slice_and_send(
            input_path=payload.input_path,
            output_path=payload.output_path or None,
            profile_path=payload.profile_path or None,
            start_print=payload.start_print,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/files/directory")
def files_directory(path: str = Query(default="/")) -> List[Dict[str, object]]:
    normalized = (path or "/").strip().lower()
    use_downloads = normalized in {"", "/", "/downloads", "downloads"}
    if use_downloads:
        target = _downloads_directory().expanduser().resolve()
        if not target.exists() or not target.is_dir():
            raise HTTPException(status_code=404, detail=f"Downloads directory not found: {target}")
    else:
        target = _resolve_directory(path)

    root_for_relative = target if use_downloads else _files_root()
    rows = []
    for entry in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        try:
            size = "-" if entry.is_dir() else _human_size(entry.stat().st_size)
        except Exception:
            size = "-"

        if use_downloads:
            rel_path = "/downloads/" + entry.name
        else:
            rel_path = "/" + entry.relative_to(root_for_relative).as_posix()

        rows.append(
            {
                "name": entry.name,
                "size": size,
                "is_folder": entry.is_dir(),
                "path": rel_path,
            }
        )
    return rows[:100]


@app.get("/api/files/recent-downloads")
def files_recent_downloads(limit: int = Query(default=15, ge=1, le=100)) -> List[Dict[str, object]]:
    downloads = _downloads_directory()
    if not downloads.exists() or not downloads.is_dir():
        raise HTTPException(status_code=404, detail=f"Downloads directory not found: {downloads}")

    rows: List[Dict[str, object]] = []
    for entry in downloads.iterdir():
        if not entry.is_file():
            continue
        try:
            stat = entry.stat()
            rows.append(
                {
                    "name": entry.name,
                    "size": _human_size(stat.st_size),
                    "modified": _format_relative_modified(stat.st_mtime),
                    "path": str(entry),
                    "_mtime": stat.st_mtime,
                }
            )
        except Exception:
            continue

    rows.sort(key=lambda row: row.get("_mtime", 0), reverse=True)
    trimmed = rows[:limit]
    for row in trimmed:
        row.pop("_mtime", None)
    return trimmed


@app.post("/api/files/write")
def files_write(payload: FileWriteRequest) -> Dict[str, object]:
    target = _resolve_file(payload.path)
    existed = target.exists()
    if target.exists() and target.is_dir():
        raise HTTPException(status_code=400, detail="Target path points to a directory")
    if target.exists() and not payload.overwrite:
        raise HTTPException(status_code=409, detail="File already exists. Set overwrite=true to replace it")

    target.parent.mkdir(parents=True, exist_ok=True)
    data = payload.content or ""
    target.write_text(data, encoding="utf-8")
    return {
        "path": str(target),
        "bytes": len(data.encode("utf-8")),
        "overwritten": bool(payload.overwrite and existed),
    }


@app.get("/api/maps/location")
def maps_location() -> Dict[str, object]:
    return {
        "label": os.getenv("FUTURE_LOCATION_LABEL", "Home Workshop"),
        "address": os.getenv("FUTURE_LOCATION_ADDRESS", "Lonsdale, MN"),
        "lat": _safe_float(os.getenv("FUTURE_LOCATION_LAT", "44.480"), 44.480),
        "lng": _safe_float(os.getenv("FUTURE_LOCATION_LNG", "-93.430"), -93.430),
    }


@app.post("/api/chat/message")
def chat_message(payload: ChatMessageRequest) -> Dict[str, str]:
    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is required")

    try:
        recent_context = list(CHAT_CONTEXT_LINES)
        reply = _chat_reply(message, recent_context=recent_context)
        normalized = _normalize_chat_reply(reply)
        CHAT_CONTEXT_LINES.append({"role": "user", "content": message})
        CHAT_CONTEXT_LINES.append({"role": "assistant", "content": normalized})
        return {"reply": normalized}
    except Exception:
        fallback = handle_gmail_command(message)
        normalized = _normalize_chat_reply(fallback)
        CHAT_CONTEXT_LINES.append({"role": "user", "content": message})
        CHAT_CONTEXT_LINES.append({"role": "assistant", "content": normalized})
        return {"reply": normalized}


@app.post("/api/self-update/plan")
def self_update_plan_endpoint(payload: SelfUpdatePlanRequest) -> Dict[str, object]:
    result = self_update_plan(
        instruction=payload.instruction,
        target_files=payload.target_files,
        scope=payload.scope,
    )
    if result.get("status") != "ok":
        raise HTTPException(status_code=400, detail=str(result.get("error", "Self-update planning failed")))
    return result


@app.post("/api/self-update/execute")
def self_update_execute_endpoint() -> Dict[str, object]:
    result = self_update_execute_latest()
    if result.get("status") != "ok":
        raise HTTPException(status_code=400, detail=str(result.get("error", "Self-update execute failed")))
    return result


@app.post("/api/tts/elevenlabs")
def elevenlabs_tts(payload: TtsRequest) -> Response:
    text = str(payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text is required")

    # Speak an intro preview for long responses so voice mode remains snappy.
    bounded_text = _tts_intro_preview(text)
    try:
        audio_bytes = _synthesize_elevenlabs_audio(bounded_text)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"TTS failed: {exc}") from exc

    return Response(content=audio_bytes, media_type="audio/mpeg")


@app.post("/api/connect/spotify")
def connect_spotify() -> Dict[str, str]:
    try:
        run_spotify_callback_server()
    except Exception:
        # Return auth URL even if listener startup fails, so manual code exchange remains possible.
        pass
    return {"auth_url": get_spotify_auth_url()}


@app.post("/api/connect/google-calendar")
def connect_google_calendar() -> Dict[str, str]:
    return {"auth_url": get_google_auth_url()}


@app.get("/oauth/spotify/callback", response_class=HTMLResponse)
def spotify_oauth_callback(code: str = "", error: str = "") -> HTMLResponse:
    if error:
        return HTMLResponse(f"<h1>Spotify authorization failed</h1><p>{error}</p>", status_code=400)
    if not code:
        return HTMLResponse("<h1>Spotify authorization failed</h1><p>Missing code</p>", status_code=400)

    try:
        token_data = exchange_spotify_code(code)
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        if not access_token:
            return HTMLResponse("<h1>Spotify authorization failed</h1><p>No access token returned</p>", status_code=500)
        set_spotify_tokens(access_token, refresh_token)
    except Exception as exc:
        return HTMLResponse(f"<h1>Spotify authorization failed</h1><p>{exc}</p>", status_code=500)

    return HTMLResponse("<h1>Spotify connected</h1><p>You can close this tab.</p>")


@app.get("/oauth/google/callback", response_class=HTMLResponse)
@app.get("/callback", response_class=HTMLResponse)
def google_oauth_callback(code: str = "", error: str = "") -> HTMLResponse:
    if error:
        return HTMLResponse(f"<h1>Google authorization failed</h1><p>{error}</p>", status_code=400)
    if not code:
        return HTMLResponse("<h1>Google authorization failed</h1><p>Missing code</p>", status_code=400)

    try:
        complete_google_calendar_authorization(code)
    except Exception as exc:
        return HTMLResponse(f"<h1>Google authorization failed</h1><p>{exc}</p>", status_code=500)

    return HTMLResponse(
        "<html><body style='font-family:sans-serif;background:#0a0b0c;color:#e9e6e0;display:flex;"
        "align-items:center;justify-content:center;height:100vh;margin:0'>"
        "<div style='text-align:center'><h1 style='color:#6fa677'>Google Connected</h1>"
        "<p>Calendar and Gmail are now live. You can close this tab.</p></div></body></html>"
    )


@app.post("/vscode/open")
async def open_in_vscode(file_path: str = Query(...), line: int = Query(1)):
    """Open a file in VS Code at specified line for self-update review."""
    try:
        abs_path = Path(file_path).resolve()
        if not abs_path.exists():
            raise HTTPException(status_code=404, detail=f"File not found: {file_path}")
        
        if platform.system() == "Windows":
            path_str = str(abs_path).replace("\\", "/")
        else:
            path_str = str(abs_path)
        
        vscode_uri = f"vscode://file/{path_str}:{line}"
        webbrowser.open(vscode_uri)
        
        return {
            "status": "success",
            "uri": vscode_uri,
            "file": str(abs_path),
            "line": line
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/self-update/plan-and-open")
async def self_update_plan_and_open(request: SelfUpdatePlanRequest):
    """Generate self-update plan and open affected files in VS Code."""
    try:
        plan = self_update_plan(
            instruction=request.instruction,
            target_files=request.target_files,
            scope=request.scope
        )
        
        vscode_uris = []
        if plan and "edits" in plan:
            for edit in plan["edits"]:
                if "path" in edit:
                    file_path = Path(edit["path"]).resolve()
                    if file_path.exists():
                        if platform.system() == "Windows":
                            path_str = str(file_path).replace("\\", "/")
                        else:
                            path_str = str(file_path)
                        uri = f"vscode://file/{path_str}:1"
                        vscode_uris.append({"file": edit["path"], "uri": uri})
                        webbrowser.open(uri)
        
        return {
            "status": "success",
            "plan": plan,
            "opened_in_vscode": vscode_uris
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
