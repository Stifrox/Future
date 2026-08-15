import os
import subprocess
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional

import requests

SLICER_PATH_CANDIDATES = [
    r"C:\Program Files\AnycubicSlicer\AnycubicSlicer.exe",
    r"C:\Program Files (x86)\AnycubicSlicer\AnycubicSlicer.exe",
    r"C:\Program Files\OrcaSlicer\orca-slicer.exe",
    r"C:\Program Files\Prusa3D\PrusaSlicer\prusa-slicer.exe",
]


def _as_int(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return default


def _as_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _resolve_slicer_path() -> Optional[Path]:
    env_path = os.getenv("FUTURE_SLICER_PATH", "").strip().strip('"')
    if env_path:
        path = Path(env_path)
        if path.exists():
            return path

    for candidate in SLICER_PATH_CANDIDATES:
        path = Path(candidate)
        if path.exists():
            return path

    return None


def _build_slicer_command(
    slicer_path: Path,
    input_model: Path,
    output_gcode: Path,
    profile_path: Optional[Path] = None,
    extra_args: Optional[List[str]] = None,
) -> List[str]:
    template = os.getenv("ANYCUBIC_SLICE_COMMAND_TEMPLATE", "").strip()
    if template:
        # Example template:
        # "{slicer}" --export-gcode "{input}" --output "{output}" --load "{profile}"
        rendered = template.format(
            slicer=str(slicer_path),
            input=str(input_model),
            output=str(output_gcode),
            profile=str(profile_path or ""),
        )
        return ["cmd", "/c", rendered]

    command = [str(slicer_path), "--export-gcode", str(input_model), "--output", str(output_gcode)]
    if profile_path:
        command.extend(["--load", str(profile_path)])
    if extra_args:
        command.extend(extra_args)
    return command


def slice_model(
    input_path: str,
    output_path: Optional[str] = None,
    profile_path: Optional[str] = None,
    extra_args: Optional[List[str]] = None,
) -> Dict[str, object]:
    input_model = Path(input_path).expanduser().resolve()
    if not input_model.exists() or not input_model.is_file():
        raise FileNotFoundError(f"Input model not found: {input_model}")

    output_gcode = (
        Path(output_path).expanduser().resolve()
        if output_path
        else input_model.with_suffix(".gcode")
    )
    output_gcode.parent.mkdir(parents=True, exist_ok=True)

    slicer_path = _resolve_slicer_path()
    if slicer_path is None:
        raise RuntimeError("No slicer executable found. Set FUTURE_SLICER_PATH.")

    profile = Path(profile_path).expanduser().resolve() if profile_path else None
    if profile and not profile.exists():
        raise FileNotFoundError(f"Slicer profile not found: {profile}")

    command = _build_slicer_command(slicer_path, input_model, output_gcode, profile, extra_args)
    process = subprocess.run(command, capture_output=True, text=True, timeout=_as_int(os.getenv("ANYCUBIC_SLICE_TIMEOUT_SEC", "1800"), 1800))

    if process.returncode != 0:
        raise RuntimeError(
            "Slicing failed: "
            + (process.stderr.strip() or process.stdout.strip() or f"exit code {process.returncode}")
        )

    if not output_gcode.exists():
        raise RuntimeError("Slicer command finished but output G-code file was not created.")

    return {
        "slicer": str(slicer_path),
        "input_model": str(input_model),
        "output_gcode": str(output_gcode),
        "status": "sliced",
    }


def _printer_backend() -> str:
    return os.getenv("ANYCUBIC_BACKEND", "octoprint").strip().lower()


def _printer_base_url() -> str:
    base = os.getenv("ANYCUBIC_BASE_URL", "").strip().rstrip("/")
    if not base:
        raise RuntimeError("ANYCUBIC_BASE_URL is not configured.")
    return base


def _printer_headers() -> Dict[str, str]:
    api_key = os.getenv("ANYCUBIC_API_KEY", "").strip()
    token = os.getenv("ANYCUBIC_BEARER_TOKEN", "").strip()

    headers: Dict[str, str] = {}
    if api_key:
        headers["X-Api-Key"] = api_key
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _upload_octoprint(gcode_path: Path, start_print: bool) -> Dict[str, object]:
    base = _printer_base_url()
    headers = _printer_headers()
    with gcode_path.open("rb") as handle:
        response = requests.post(
            f"{base}/api/files/local",
            headers=headers,
            data={"select": "true", "print": "true" if start_print else "false"},
            files={"file": (gcode_path.name, handle, "application/octet-stream")},
            timeout=_as_int(os.getenv("ANYCUBIC_UPLOAD_TIMEOUT_SEC", "120"), 120),
        )
    response.raise_for_status()
    payload = response.json() if response.text else {}
    return {
        "backend": "octoprint",
        "uploaded": True,
        "started": bool(start_print),
        "file": gcode_path.name,
        "remote": payload,
    }


def _upload_moonraker(gcode_path: Path, start_print: bool) -> Dict[str, object]:
    base = _printer_base_url()
    headers = _printer_headers()
    folder = os.getenv("ANYCUBIC_MOONRAKER_FOLDER", "gcodes").strip() or "gcodes"

    with gcode_path.open("rb") as handle:
        response = requests.post(
            f"{base}/server/files/upload",
            headers=headers,
            data={"root": folder, "path": ""},
            files={"file": (gcode_path.name, handle, "application/octet-stream")},
            timeout=_as_int(os.getenv("ANYCUBIC_UPLOAD_TIMEOUT_SEC", "120"), 120),
        )
    response.raise_for_status()

    started = False
    if start_print:
        filename = urllib.parse.quote(gcode_path.name)
        start_response = requests.post(
            f"{base}/printer/print/start?filename={filename}",
            headers=headers,
            timeout=_as_int(os.getenv("ANYCUBIC_UPLOAD_TIMEOUT_SEC", "120"), 120),
        )
        start_response.raise_for_status()
        started = True

    payload = response.json() if response.text else {}
    return {
        "backend": "moonraker",
        "uploaded": True,
        "started": started,
        "file": gcode_path.name,
        "remote": payload,
    }


def upload_gcode(gcode_path: str, start_print: bool = False) -> Dict[str, object]:
    local = Path(gcode_path).expanduser().resolve()
    if not local.exists() or not local.is_file():
        raise FileNotFoundError(f"G-code file not found: {local}")

    backend = _printer_backend()
    if backend == "octoprint":
        return _upload_octoprint(local, start_print)
    if backend == "moonraker":
        return _upload_moonraker(local, start_print)

    raise RuntimeError("ANYCUBIC_BACKEND must be 'octoprint' or 'moonraker'.")


def fetch_print_status() -> Dict[str, object]:
    backend = _printer_backend()
    base = _printer_base_url()
    headers = _printer_headers()

    if backend == "octoprint":
        response = requests.get(
            f"{base}/api/job",
            headers=headers,
            timeout=_as_int(os.getenv("ANYCUBIC_STATUS_TIMEOUT_SEC", "30"), 30),
        )
        response.raise_for_status()
        payload = response.json() or {}
        progress = payload.get("progress", {})
        job = payload.get("job", {})
        state = (payload.get("state") or "").lower()
        file_name = ((job.get("file") or {}).get("name")) or "Unknown.gcode"

        nozzle_temp = 0.0
        if _bool_env("ANYCUBIC_FETCH_TEMPS", False):
            temp_response = requests.get(
                f"{base}/api/printer",
                headers=headers,
                params={"exclude": "sd,state"},
                timeout=_as_int(os.getenv("ANYCUBIC_STATUS_TIMEOUT_SEC", "30"), 30),
            )
            temp_response.raise_for_status()
            temp_payload = temp_response.json() or {}
            nozzle_temp = _as_float(((temp_payload.get("temperature") or {}).get("tool0") or {}).get("actual"), 0.0)

        completion = _as_float(progress.get("completion"), 0.0)
        print_time_left = _as_int(progress.get("printTimeLeft"), 0)
        elapsed_sec = _as_int(progress.get("printTime"), 0)
        layer = _as_int(completion / 100.0 * 1000, 0)

        return {
            "printing": "print" in state or state in {"printing", "paused"},
            "file": file_name,
            "layer": layer,
            "layer_total": 1000,
            "progress_pct": round(completion, 2),
            "eta": f"{max(0, print_time_left // 60)}m",
            "nozzle_temp": round(nozzle_temp, 1),
            "elapsed_sec": elapsed_sec,
            "backend": "octoprint",
        }

    if backend == "moonraker":
        response = requests.get(
            f"{base}/printer/objects/query?print_stats&extruder",
            headers=headers,
            timeout=_as_int(os.getenv("ANYCUBIC_STATUS_TIMEOUT_SEC", "30"), 30),
        )
        response.raise_for_status()
        payload = response.json() or {}
        status = ((payload.get("result") or {}).get("status") or {})
        print_stats = status.get("print_stats") or {}
        extruder = status.get("extruder") or {}

        filename = print_stats.get("filename") or "Unknown.gcode"
        state = (print_stats.get("state") or "").lower()
        total_duration = _as_float(print_stats.get("total_duration"), 0.0)
        progress = _as_float(print_stats.get("progress"), 0.0) * 100.0
        eta_minutes = _as_int(total_duration * (1.0 - min(1.0, max(0.0, progress / 100.0))) / 60.0, 0)

        return {
            "printing": state in {"printing", "paused"},
            "file": filename,
            "layer": 0,
            "layer_total": 0,
            "progress_pct": round(progress, 2),
            "eta": f"{max(0, eta_minutes)}m",
            "nozzle_temp": round(_as_float(extruder.get("temperature"), 0.0), 1),
            "elapsed_sec": _as_int(total_duration, 0),
            "backend": "moonraker",
        }

    raise RuntimeError("ANYCUBIC_BACKEND must be 'octoprint' or 'moonraker'.")


def slice_and_send(
    input_path: str,
    output_path: Optional[str] = None,
    profile_path: Optional[str] = None,
    start_print: bool = True,
    extra_args: Optional[List[str]] = None,
) -> Dict[str, object]:
    sliced = slice_model(
        input_path=input_path,
        output_path=output_path,
        profile_path=profile_path,
        extra_args=extra_args,
    )
    uploaded = upload_gcode(sliced["output_gcode"], start_print=start_print)
    return {
        "status": "ok",
        "slice": sliced,
        "upload": uploaded,
    }
