"""File-backed storage and connection helpers for WiFi devices (Raspberry Pi, ESP32, etc)."""
import json
import socket
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

_APP_ROOT = Path(__file__).resolve().parent.parent
DEVICES_FILE = _APP_ROOT / "data" / "devices.json"

VALID_TYPES = {"raspberry_pi", "esp32"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_all() -> List[Dict]:
    if not DEVICES_FILE.exists():
        return []
    try:
        payload = json.loads(DEVICES_FILE.read_text(encoding="utf-8"))
        return payload.get("devices", []) if isinstance(payload, dict) else []
    except Exception:
        return []


def _save_all(devices: List[Dict]) -> None:
    DEVICES_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEVICES_FILE.write_text(json.dumps({"devices": devices}, indent=2), encoding="utf-8")


def list_devices(device_type: Optional[str] = None) -> List[Dict]:
    devices = _load_all()
    if device_type:
        devices = [d for d in devices if d.get("type") == device_type]
    return devices


def get_device(device_id: str) -> Optional[Dict]:
    for device in _load_all():
        if device.get("id") == device_id:
            return device
    return None


def _clean_commands(commands: Optional[List[Dict]]) -> List[Dict]:
    cleaned = []
    for entry in commands or []:
        if not isinstance(entry, dict):
            continue
        trigger = str(entry.get("trigger", "")).strip()
        path = str(entry.get("path", "")).strip()
        if not trigger or not path:
            continue
        cleaned.append({
            "trigger": trigger,
            "path": path if path.startswith("/") else "/" + path,
            "method": str(entry.get("method", "GET")).strip().upper() or "GET",
            "body": entry.get("body") if isinstance(entry.get("body"), dict) else None,
        })
    return cleaned


def add_device(
    name: str,
    device_type: str,
    host: str,
    port: int,
    stream_path: str = "",
    api_key: str = "",
    description: str = "",
    commands: Optional[List[Dict]] = None,
) -> Dict:
    if device_type not in VALID_TYPES:
        raise ValueError(f"Unsupported device type: {device_type}")
    if not host.strip():
        raise ValueError("Device host/IP is required")

    devices = _load_all()
    device = {
        "id": uuid.uuid4().hex[:12],
        "name": (name or "").strip() or f"{device_type.replace('_', ' ').title()} Device",
        "type": device_type,
        "host": host.strip(),
        "port": int(port) if port else (80 if device_type == "esp32" else 8080),
        "stream_path": stream_path.strip(),
        "api_key": api_key.strip(),
        "description": description.strip(),
        "commands": _clean_commands(commands),
        "status": "unknown",
        "created": _now_iso(),
        "last_checked": None,
    }
    devices.append(device)
    _save_all(devices)
    return device


def update_device(device_id: str, **fields) -> Optional[Dict]:
    devices = _load_all()
    for device in devices:
        if device.get("id") == device_id:
            for key in ("name", "host", "port", "stream_path", "api_key", "description"):
                if key in fields and fields[key] is not None:
                    device[key] = fields[key]
            if "commands" in fields and fields["commands"] is not None:
                device["commands"] = _clean_commands(fields["commands"])
            _save_all(devices)
            return device
    return None


def delete_device(device_id: str) -> bool:
    devices = _load_all()
    remaining = [d for d in devices if d.get("id") != device_id]
    if len(remaining) == len(devices):
        return False
    _save_all(remaining)
    return True


def _base_url(device: Dict) -> str:
    return f"http://{device['host']}:{device['port']}"


def camera_stream_url(device: Dict) -> str:
    path = device.get("stream_path") or "/stream.mjpg"
    if not path.startswith("/"):
        path = "/" + path
    return f"{_base_url(device)}{path}"


def test_connection(device_id: str, timeout: float = 4.0) -> Dict:
    device = get_device(device_id)
    if not device:
        raise ValueError("Device not found")

    host, port = device["host"], int(device["port"])
    result = {"id": device_id, "reachable": False, "status": "offline", "detail": ""}

    try:
        with socket.create_connection((host, port), timeout=timeout):
            result["reachable"] = True
    except OSError as exc:
        result["detail"] = f"Could not open TCP connection to {host}:{port} ({exc})"
        result["status"] = "offline"
        _mark_status(device_id, "offline")
        return result

    # TCP reachable - try an HTTP probe for a richer status where possible.
    try:
        headers = {"Authorization": f"Bearer {device['api_key']}"} if device.get("api_key") else {}
        resp = requests.get(_base_url(device) + "/", headers=headers, timeout=timeout)
        result["detail"] = f"HTTP {resp.status_code} from {host}:{port}"
    except Exception:
        result["detail"] = f"TCP port {port} is open on {host}, but no HTTP response was received."

    result["status"] = "online"
    _mark_status(device_id, "online")
    return result


def _mark_status(device_id: str, status: str) -> None:
    devices = _load_all()
    for device in devices:
        if device.get("id") == device_id:
            device["status"] = status
            device["last_checked"] = _now_iso()
            break
    _save_all(devices)


def send_command(device_id: str, path: str, method: str = "GET", body: Optional[Dict] = None, timeout: float = 6.0) -> Dict:
    """Send an HTTP command to an ESP32 (or any device exposing an HTTP API), e.g. /led/on."""
    device = get_device(device_id)
    if not device:
        raise ValueError("Device not found")

    clean_path = path if path.startswith("/") else "/" + path
    url = _base_url(device) + clean_path
    headers = {"Authorization": f"Bearer {device['api_key']}"} if device.get("api_key") else {}
    method = (method or "GET").upper()

    try:
        if method == "POST":
            resp = requests.post(url, json=body or {}, headers=headers, timeout=timeout)
        else:
            resp = requests.get(url, headers=headers, timeout=timeout)
        try:
            data = resp.json()
        except Exception:
            data = resp.text[:2000]
        return {"ok": resp.ok, "status_code": resp.status_code, "response": data}
    except Exception as exc:
        return {"ok": False, "status_code": None, "response": str(exc)}


_STOPWORDS = {
    "the", "a", "an", "my", "of", "to", "is", "at", "in", "on", "please",
    "hey", "future", "can", "you", "for", "me", "and", "with", "over", "wifi",
}


def _keywords(text: str) -> set:
    words = set(w.strip(".,!?'\"") for w in (text or "").lower().split())
    return {w for w in words if w and w not in _STOPWORDS}


def describe_devices_for_prompt() -> str:
    """Summarize connected devices so the model knows what Future can see/control."""
    devices = _load_all()
    if not devices:
        return "No WiFi devices (Raspberry Pi / ESP32) are connected yet."

    lines = []
    for device in devices:
        kind = "Raspberry Pi camera" if device["type"] == "raspberry_pi" else "ESP32 board"
        desc = device.get("description") or "no description set"
        line = f"- \"{device['name']}\" ({kind}) at {device['host']}:{device['port']} - {desc}"
        commands = device.get("commands") or []
        if commands:
            triggers = ", ".join(f"'{c['trigger']}'" for c in commands)
            line += f" | Known voice commands: {triggers}"
        lines.append(line)
    return "Connected WiFi devices:\n" + "\n".join(lines)


def match_camera_device(query: str) -> Optional[Dict]:
    """Find the Raspberry Pi camera device that best matches a natural-language request."""
    cameras = list_devices("raspberry_pi")
    if not cameras:
        return None
    if len(cameras) == 1:
        return cameras[0]

    query_words = _keywords(query)
    best, best_score = None, 0
    for device in cameras:
        haystack = _keywords(f"{device['name']} {device.get('description', '')}")
        score = len(query_words & haystack)
        if score > best_score:
            best, best_score = device, score
    return best or cameras[0]


def match_device_command(query: str) -> Optional[Dict]:
    """Find an ESP32 device + saved command whose trigger phrase appears in the query."""
    query_lower = (query or "").lower()
    for device in list_devices("esp32"):
        for command in device.get("commands") or []:
            if command["trigger"].lower() in query_lower:
                return {"device": device, "command": command}
    return None

