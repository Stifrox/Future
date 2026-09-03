"""Overnight Instagram content pipeline: scan a source folder of clips/screenshots,
generate short scripted videos with voiceover + captions, and email the batch for approval.

Runs as a background task (see tools/tasks.py, type "instagram_batch"). Nothing here
runs unless explicitly triggered ("activate content creation" or POST /api/content/run).
"""
import json
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

import config
from tools.integrations import (
    _load_email_contacts,
    get_gmail_message_body,
    list_gmail_messages,
    send_gmail_message_with_attachments,
)
from tools.tasks import update_task

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi"}
OUTPUT_ROOT = Path("generated") / "instagram"
DEFAULT_CLIP_SECONDS = 20
MIN_BATCH_SIZE = 6
MAX_BATCH_SIZE = 20
SUBJECT_TAG_RE = re.compile(r"FUTURE-CONTENT #([a-f0-9]{8})", re.IGNORECASE)
MAX_EMAIL_ATTACHMENT_BYTES = 20 * 1024 * 1024  # stay comfortably under Gmail's ~25MB cap


def _resolve_source_dir(override: Optional[str] = None) -> Optional[Path]:
    raw = (override or config.CONTENT_SOURCE_DIR or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_dir() else None


def scan_source_assets(source_dir: Path) -> List[Dict[str, str]]:
    assets = []
    for path in sorted(source_dir.rglob("*")):
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext in IMAGE_EXTS:
            assets.append({"path": str(path), "name": path.stem, "kind": "image"})
        elif ext in VIDEO_EXTS:
            assets.append({"path": str(path), "name": path.stem, "kind": "video"})
    return assets


def _resolve_owner_email() -> Optional[str]:
    configured = (config.CONTENT_APPROVAL_EMAIL or "").strip()
    if configured:
        return configured
    contacts = _load_email_contacts()
    if len(contacts) == 1:
        return next(iter(contacts.values()))
    return None


def _generate_text(prompt: str, max_tokens: int = 800) -> str:
    """Minimal, self-contained model call (kept independent from webtools to avoid a circular import)."""
    api_key = (config.OPENAI_API_KEY or "").strip()
    if api_key and OpenAI:
        try:
            client = OpenAI(api_key=api_key)
            response = client.chat.completions.create(
                model=(config.PRIMARY_MODEL or "gpt-4o-mini").strip(),
                messages=[{"role": "user", "content": prompt}],
                max_completion_tokens=max_tokens,
            )
            text = (response.choices[0].message.content or "").strip()
            if text:
                return text
        except Exception as exc:
            print(f"Content studio OpenAI call failed: {exc}")

    anthropic_key = (config.ANTHROPIC_API_KEY or "").strip()
    if anthropic_key:
        try:
            response = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": anthropic_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": config.ANTHROPIC_MODEL or "claude-sonnet-4-5",
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=60,
            )
            response.raise_for_status()
            pieces = [
                block.get("text", "")
                for block in response.json().get("content", [])
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            text = "\n".join(pieces).strip()
            if text:
                return text
        except Exception as exc:
            print(f"Content studio Anthropic call failed: {exc}")

    raise RuntimeError("No text-generation model is configured (set OPENAI_API_KEY or ANTHROPIC_API_KEY)")


def _target_batch_size(asset_count: int, requested: int = 18) -> int:
    if asset_count <= 0:
        return 0
    # Roughly 2-4 assets per video; don't promise more videos than the material supports.
    supportable = max(1, asset_count // 2)
    return max(MIN_BATCH_SIZE, min(requested, MAX_BATCH_SIZE, supportable))


def generate_batch_script_plan(assets: List[Dict[str, str]], target_count: int, instruction: str = "") -> List[Dict[str, object]]:
    asset_names = [asset["name"] for asset in assets]
    direction = (instruction or "").strip()
    direction_line = f"Follow this creative direction from the user: {direction} " if direction else ""
    plan = []
    for first_index in range(0, target_count, 3):
        count = min(3, target_count - first_index)
        prompt = (
            "You are writing short-form Instagram Reel scripts promoting a personal app project. "
            f"Source clip/screenshot filenames, used only as topic hints: {asset_names}. "
            f"Write exactly {count} distinct videos, each 15-30 seconds when read aloud (40-55 words). "
            "Use distinct feature, behind-the-scenes, tip, or relatable-hook angles. "
            f"{direction_line}"
            "Return ONLY a valid JSON array, no markdown or commentary. Each item must have: "
            '"topic", "script", "caption", and "hashtags" (an array of 5-8 strings without #).'
        )
        raw = _generate_text(prompt, max_tokens=1400).strip()
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
        start, end = cleaned.find("["), cleaned.rfind("]")
        if start < 0 or end <= start:
            raise RuntimeError("The script planner did not return a complete JSON list.")
        try:
            group = json.loads(cleaned[start:end + 1])
        except Exception as exc:
            raise RuntimeError(f"Could not parse a script-planning group as JSON: {exc}") from exc
        if not isinstance(group, list) or len(group) != count:
            raise RuntimeError("The script planner returned an incomplete script-planning group.")
        plan.extend(group)

    specs = []
    for index, item in enumerate(plan[:target_count]):
        if not isinstance(item, dict):
            continue
        assigned = [assets[i % len(assets)]["path"] for i in range(index * 3, index * 3 + 3)] if assets else []
        specs.append(
            {
                "index": index,
                "topic": str(item.get("topic", f"Video {index + 1}")).strip(),
                "script": str(item.get("script", "")).strip(),
                "caption": str(item.get("caption", "")).strip(),
                "hashtags": [str(tag).strip().lstrip("#") for tag in item.get("hashtags", []) if str(tag).strip()],
                "assets": assigned,
            }
        )
    return specs


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _run_ffmpeg(args: List[str]) -> None:
    result = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr[:800]}")


def _media_duration_seconds(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True,
        text=True,
    )
    try:
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def _normalize_clip(asset: Dict[str, str], duration: float, out_path: Path) -> None:
    scale_pad = "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1"
    if asset["kind"] == "image":
        _run_ffmpeg([
            "-loop", "1", "-i", asset["path"], "-t", f"{duration:.2f}",
            "-vf", scale_pad, "-r", "30", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_path),
        ])
    else:
        _run_ffmpeg([
            "-stream_loop", "-1", "-i", asset["path"], "-t", f"{duration:.2f}",
            "-vf", scale_pad, "-r", "30", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_path),
        ])


def _concat_clips(clip_paths: List[Path], out_path: Path, work_dir: Path) -> None:
    list_file = work_dir / "concat_list.txt"
    list_file.write_text("\n".join(f"file '{p.resolve()}'" for p in clip_paths), encoding="utf-8")
    _run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(out_path)])


def _build_srt(script_text: str, total_duration: float, out_path: Path) -> None:
    words = script_text.split()
    chunk_size = 8
    chunks = [" ".join(words[i:i + chunk_size]) for i in range(0, len(words), chunk_size)] or [script_text]
    per_chunk = max(0.8, total_duration / max(1, len(chunks)))

    def _fmt(t: float) -> str:
        hours, rem = divmod(max(0.0, t), 3600)
        minutes, seconds = divmod(rem, 60)
        millis = int((seconds - int(seconds)) * 1000)
        return f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d},{millis:03d}"

    lines = []
    for i, chunk in enumerate(chunks):
        start = i * per_chunk
        end = min(total_duration, start + per_chunk)
        lines.append(f"{i + 1}\n{_fmt(start)} --> {_fmt(end)}\n{chunk}\n")
    out_path.write_text("\n".join(lines), encoding="utf-8")


def _synthesize_voiceover(text: str, out_path: Path) -> None:
    api_key = (config.ELEVENLABS_API_KEY or "").strip()
    voice_id = (config.ELEVENLABS_VOICE_ID or "").strip()
    if not api_key or not voice_id:
        raise RuntimeError("ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID must be configured")

    response = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        headers={"xi-api-key": api_key, "accept": "audio/mpeg", "content-type": "application/json"},
        json={"text": text, "model_id": "eleven_turbo_v2_5", "output_format": "mp3_44100_128"},
        timeout=60,
    )
    if not response.ok or not response.content:
        raise RuntimeError(f"ElevenLabs TTS failed ({response.status_code}): {response.text[:300]}")
    out_path.write_bytes(response.content)


def assemble_video(spec: Dict[str, object], assets_by_path: Dict[str, Dict[str, str]], work_dir: Path) -> Dict[str, str]:
    work_dir.mkdir(parents=True, exist_ok=True)
    voice_path = work_dir / "voice.mp3"
    _synthesize_voiceover(str(spec["script"]) or str(spec["topic"]), voice_path)
    duration = _media_duration_seconds(voice_path) or DEFAULT_CLIP_SECONDS

    asset_paths = [assets_by_path[p] for p in spec["assets"] if p in assets_by_path] or list(assets_by_path.values())[:1]
    per_clip_duration = duration / max(1, len(asset_paths))

    clip_paths = []
    for i, asset in enumerate(asset_paths):
        clip_out = work_dir / f"clip_{i}.mp4"
        _normalize_clip(asset, per_clip_duration, clip_out)
        clip_paths.append(clip_out)

    silent_video = work_dir / "silent.mp4"
    if len(clip_paths) == 1:
        shutil.copy(clip_paths[0], silent_video)
    else:
        _concat_clips(clip_paths, silent_video, work_dir)

    srt_path = work_dir / "captions.srt"
    _build_srt(str(spec["script"]), duration, srt_path)

    final_path = work_dir / "final.mp4"
    escaped_srt = str(srt_path).replace("\\", "/").replace(":", "\\:")
    _run_ffmpeg([
        "-i", str(silent_video), "-i", str(voice_path),
        "-vf", f"subtitles='{escaped_srt}'",
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-c:a", "aac", "-shortest", str(final_path),
    ])

    thumbnail_path = work_dir / "thumbnail.jpg"
    _run_ffmpeg(["-i", str(final_path), "-ss", "00:00:01", "-frames:v", "1", str(thumbnail_path)])

    return {"video_path": str(final_path), "thumbnail_path": str(thumbnail_path)}


def _manifest_path(batch_id: str) -> Path:
    return OUTPUT_ROOT / batch_id / "manifest.json"


def _save_manifest(manifest: Dict[str, object]) -> None:
    path = _manifest_path(str(manifest["batch_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _load_manifest(batch_id: str) -> Optional[Dict[str, object]]:
    path = _manifest_path(batch_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def send_batch_email(manifest: Dict[str, object]) -> bool:
    to_email = _resolve_owner_email()
    if not to_email:
        print("Content studio: no approval email configured, skipping send.")
        return False

    batch_id = str(manifest["batch_id"])
    lines = [
        f"Batch #{batch_id[:8]} is ready \u2014 {len(manifest['videos'])} videos.",
        "Reply to this email with: 'approve 1,3,5', 'approve all', or 'reject 2,4'.",
        "",
    ]
    attachments = []
    total_bytes = 0
    for video in manifest["videos"]:
        lines.append(f"{video['index'] + 1}. {video['topic']}")
        lines.append(f"   Caption: {video['caption']}")
        lines.append(f"   Hashtags: {' '.join('#' + tag for tag in video['hashtags'])}")
        lines.append(f"   File: {video['video_path']}")
        lines.append("")

        video_path = Path(video["video_path"])
        if video_path.exists():
            size = video_path.stat().st_size
            if total_bytes + size <= MAX_EMAIL_ATTACHMENT_BYTES:
                attachments.append((video_path.name, video_path.read_bytes(), "video/mp4"))
                total_bytes += size

    if not attachments:
        lines.append("(Videos were too large to attach directly \u2014 open them from the file paths above.)")

    subject = f"[FUTURE-CONTENT #{batch_id[:8]}] {len(manifest['videos'])} Instagram videos ready for review"
    send_gmail_message_with_attachments(to_email, subject, "\n".join(lines), attachments)
    return True


def run_content_creation_batch(payload: Dict[str, object]) -> Dict[str, object]:
    """Background task handler registered as type 'instagram_batch'."""
    task_id = str(payload.get("_task_id") or "")

    def report(current: int, total: int, message: str) -> None:
        if task_id:
            update_task(task_id, current, total, message)

    report(0, 0, "Scanning the content folder")
    source_dir = _resolve_source_dir(str(payload.get("source_dir") or ""))
    if not source_dir:
        raise RuntimeError("No content source folder is configured yet (set FUTURE_CONTENT_SOURCE_DIR).")

    assets = scan_source_assets(source_dir)
    if not assets:
        raise RuntimeError(f"No images/clips found in {source_dir}")

    if not _ffmpeg_available():
        raise RuntimeError("ffmpeg/ffprobe not found on PATH \u2014 install ffmpeg to assemble videos.")

    target_count = _target_batch_size(len(assets), int(payload.get("target_count", 18) or 18))
    instruction = str(payload.get("instruction", "") or "")
    report(0, target_count, f"Found {len(assets)} source files. Writing video concepts")
    specs = generate_batch_script_plan(assets, target_count, instruction=instruction)
    if not specs:
        raise RuntimeError("Script generation returned no videos")

    assets_by_path = {asset["path"]: asset for asset in assets}
    batch_id = uuid.uuid4().hex
    batch_dir = OUTPUT_ROOT / batch_id

    videos = []
    for spec in specs:
        video_work_dir = batch_dir / f"video_{spec['index']}"
        video_number = int(spec["index"]) + 1
        report(video_number - 1, len(specs), f"Creating video {video_number}/{len(specs)}")
        try:
            result = assemble_video(spec, assets_by_path, video_work_dir)
            videos.append({**spec, "status": "pending_approval", **result})
        except Exception as exc:
            videos.append({**spec, "status": "failed", "error": str(exc)})
        report(video_number, len(specs), f"Finished video {video_number}/{len(specs)}")

    manifest = {
        "batch_id": batch_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_dir": str(source_dir),
        "status": "pending_approval",
        "videos": videos,
    }
    _save_manifest(manifest)

    emailed = False
    try:
        report(len(specs), len(specs), "Videos are finished. Emailing the batch for approval")
        emailed = send_batch_email(manifest)
    except Exception as exc:
        print(f"Content studio: failed to send batch email: {exc}")

    report(len(specs), len(specs), "Finished. Results are in your email" if emailed else "Finished. Email delivery needs attention")
    return {"batch_id": batch_id, "video_count": len(videos), "emailed": emailed}


def _parse_approval_reply(body: str) -> Dict[str, object]:
    lowered = body.lower()
    if "approve all" in lowered:
        return {"approve_all": True, "approve": [], "reject": []}

    def _extract(keyword: str) -> List[int]:
        match = re.search(rf"{keyword}\s+([0-9,\s]+)", lowered)
        if not match:
            return []
        return [int(n) for n in re.findall(r"\d+", match.group(1))]

    return {"approve_all": False, "approve": _extract("approve"), "reject": _extract("reject")}


def check_batch_approvals(batch_id: Optional[str] = None) -> List[Dict[str, object]]:
    """Poll Gmail for a reply to a pending batch and update video statuses. Safe to call repeatedly."""
    if not OUTPUT_ROOT.exists():
        return []

    updated = []
    batch_dirs = [OUTPUT_ROOT / batch_id] if batch_id else list(OUTPUT_ROOT.iterdir())
    for batch_path in batch_dirs:
        manifest_file = batch_path / "manifest.json"
        if not manifest_file.exists():
            continue
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        if manifest.get("status") != "pending_approval":
            continue

        tag = f"FUTURE-CONTENT #{str(manifest['batch_id'])[:8]}"
        try:
            messages = list_gmail_messages(max_results=5, query=f"subject:\"{tag}\"")
        except Exception as exc:
            print(f"Content studio: gmail check failed: {exc}")
            continue

        reply = next((m for m in messages if SUBJECT_TAG_RE.search(m.get("subject", ""))), None)
        if not reply:
            continue

        try:
            body = get_gmail_message_body(reply["id"])
        except Exception as exc:
            print(f"Content studio: could not read reply body: {exc}")
            continue

        decision = _parse_approval_reply(body)
        approved_dir = batch_path / "approved"
        approved_dir.mkdir(exist_ok=True)

        for video in manifest["videos"]:
            index_1based = video["index"] + 1
            if decision["approve_all"] or index_1based in decision["approve"]:
                video["status"] = "approved"
                src = Path(video["video_path"])
                if src.exists():
                    shutil.copy(src, approved_dir / src.name)
            elif index_1based in decision["reject"]:
                video["status"] = "rejected"

        manifest["status"] = "reviewed"
        manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        updated.append(manifest)

    return updated
