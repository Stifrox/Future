"""Computer vision and video capture tool for Future AI assistant.

Enables Future to see through the laptop/webcam or mobile camera:
- Captures ~5-second video clips on voice/chat trigger ("look at this", "hey future look at this", etc.)
- Samples keyframes from the clip and sends multi-frame vision context to OpenAI Vision
- Generates detailed visual understanding of objects, text, code, screens, actions, and environment
- Stores detailed visual observations into Future's long-term memory for immediate help & future recall
"""

import base64
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

try:
    import cv2
except Exception:
    cv2 = None

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

from tools.memory import load_memory, remember, save_memory

try:
    import config
except Exception:
    config = None


_VISION_TRIGGER_PATTERNS = [
    re.compile(r"\b(?:hey\s+)?future\s*,?\s*look\s+at\s+this\b", re.IGNORECASE),
    re.compile(r"\blook\s+at\s+this\b", re.IGNORECASE),
    re.compile(r"\blook\s+at\s+that\b", re.IGNORECASE),
    re.compile(r"\blook\s+at\s+what\s+i(?:'?m|\s+am)?\s+showing\s+you\b", re.IGNORECASE),
    re.compile(r"\blook\s+at\s+my\s+screen\b", re.IGNORECASE),
    re.compile(r"\blook\s+at\s+(?:the\s+)?camera\b", re.IGNORECASE),
    re.compile(r"\blook\s+through\s+(?:the\s+|my\s+)?camera\b", re.IGNORECASE),
    re.compile(r"\bcan\s+you\s+see\s+this\b", re.IGNORECASE),
    re.compile(r"\btake\s+a\s+look\s+at\s+this\b", re.IGNORECASE),
    re.compile(r"\bwatch\s+this\b", re.IGNORECASE),
    re.compile(r"\bcheck\s+this\s+out\b", re.IGNORECASE),
    re.compile(r"\bsee\s+what\s+i(?:'?m|\s+am)?\s+holding\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+am\s+i\s+holding\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+is\s+in\s+front\s+of\s+(?:you|the\s+camera)\b", re.IGNORECASE),
    re.compile(r"\bvideo\s+capture\b", re.IGNORECASE),
    re.compile(r"\brecord\s+(?:a\s+)?(?:video|clip)\s+and\s+look\b", re.IGNORECASE),
]


def is_vision_query(query: str) -> bool:
    """Check if the given user query is an instruction to inspect through the camera / record a clip."""
    if not query:
        return False
    normalized = query.strip()
    return any(pattern.search(normalized) is not None for pattern in _VISION_TRIGGER_PATTERNS)


def get_video_dir() -> Path:
    """Resolve and ensure the directory where video logs and clips are stored."""
    configured = str(getattr(config, "VIDEO_PATH", "logs/video") or "logs/video") if config else "logs/video"
    path = Path(configured)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _get_vision_client() -> Optional[OpenAI]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip() or (str(getattr(config, "OPENAI_API_KEY", "")).strip() if config else "")
    if not api_key or OpenAI is None:
        return None
    try:
        return OpenAI(api_key=api_key)
    except Exception:
        return None


def _get_vision_model() -> str:
    return (
        os.getenv("FUTURE_VISION_MODEL", "").strip()
        or (str(getattr(config, "VISION_MODEL", "")).strip() if config else "")
        or "gpt-4.1-mini"
    )


def frame_to_data_url(frame, max_dimension: int = 768, quality: int = 85) -> Optional[str]:
    """Convert an OpenCV BGR numpy frame into a JPEG base64 data URL."""
    if cv2 is None or frame is None:
        return None

    try:
        height, width = frame.shape[:2]
        if max(height, width) > max_dimension:
            scale = max_dimension / float(max(height, width))
            new_w = max(1, int(width * scale))
            new_h = max(1, int(height * scale))
            frame = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)

        encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), quality]
        success, buffer = cv2.imencode(".jpg", frame, encode_param)
        if not success:
            return None
        b64 = base64.b64encode(buffer).decode("utf-8")
        return f"data:image/jpeg;base64,{b64}"
    except Exception as exc:
        print(f"[Warning] Error encoding frame to data URL: {exc}")
        return None


def capture_video_clip(
    duration: float = 5.0,
    cam_index: int = 0,
    fps: int = 15,
    res: Tuple[int, int] = (640, 480),
    sample_frames: int = 4,
    output_filename: Optional[str] = None,
) -> Dict[str, Union[bool, str, List[str], int, float]]:
    """Capture a video clip from the webcam for `duration` seconds and extract representative keyframes.
    
    Returns a dictionary with:
      - success: bool
      - video_path: str (path to saved video file)
      - frames: List[str] (base64 data URLs of keyframes sampled across the duration)
      - frame_count: int
      - duration: float
      - error: Optional[str]
    """
    if cv2 is None:
        return {
            "success": False,
            "error": "OpenCV (cv2) is not available in the current environment.",
            "frames": [],
            "video_path": "",
            "frame_count": 0,
            "duration": 0.0,
        }

    video_dir = get_video_dir()
    ts = int(time.time())
    unique_id = uuid.uuid4().hex[:6]
    final_filename = output_filename or f"clip_{ts}_{unique_id}.avi"
    video_filepath = video_dir / final_filename

    cap = None
    try:
        cap = cv2.VideoCapture(cam_index)
        if not cap.isOpened():
            # Try DSHOW or default fallback on Windows if index 0 failed
            cap.release()
            try:
                cap = cv2.VideoCapture(cam_index, cv2.CAP_DSHOW)
            except Exception:
                cap = cv2.VideoCapture(cam_index)

        if not cap.isOpened():
            return {
                "success": False,
                "error": f"Could not open camera at index {cam_index}. Make sure the webcam is connected and not in use.",
                "frames": [],
                "video_path": "",
                "frame_count": 0,
                "duration": 0.0,
            }

        # Attempt to set camera properties
        try:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, res[0])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, res[1])
            cap.set(cv2.CAP_PROP_FPS, fps)
        except Exception:
            pass

        fourcc = cv2.VideoWriter_fourcc(*"XVID")
        out = cv2.VideoWriter(str(video_filepath), fourcc, fps, res)

        captured_raw_frames = []
        start_time = time.time()
        end_time = start_time + max(1.0, float(duration))

        while time.time() < end_time:
            ret, frame = cap.read()
            if not ret or frame is None:
                time.sleep(0.03)
                continue

            out.write(frame)
            captured_raw_frames.append(frame.copy())
            time.sleep(max(0.0, (1.0 / fps) - 0.01))

        out.release()
        cap.release()
        cap = None

        total_frames = len(captured_raw_frames)
        if total_frames == 0:
            return {
                "success": False,
                "error": "No frames could be read from the camera stream.",
                "frames": [],
                "video_path": str(video_filepath),
                "frame_count": 0,
                "duration": 0.0,
            }

        # Sample evenly spaced keyframes across the captured duration
        sample_count = max(1, min(sample_frames, total_frames))
        indices = [int(i * (total_frames - 1) / (sample_count - 1)) if sample_count > 1 else 0 for i in range(sample_count)]
        
        keyframes = []
        for idx in indices:
            frame = captured_raw_frames[idx]
            data_url = frame_to_data_url(frame)
            if data_url:
                keyframes.append(data_url)

        return {
            "success": True,
            "video_path": str(video_filepath),
            "frames": keyframes,
            "frame_count": total_frames,
            "duration": round(time.time() - start_time, 2),
            "error": None,
        }

    except Exception as exc:
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
        return {
            "success": False,
            "error": f"Camera capture failed: {exc}",
            "frames": [],
            "video_path": str(video_filepath) if video_filepath.exists() else "",
            "frame_count": 0,
            "duration": 0.0,
        }


def extract_keyframes_from_video(video_path: Union[str, Path], count: int = 4) -> List[str]:
    """Extract `count` evenly spaced keyframe data URLs from an existing video file on disk."""
    if cv2 is None:
        return []

    path_obj = Path(video_path)
    if not path_obj.exists():
        return []

    cap = cv2.VideoCapture(str(path_obj))
    if not cap.isOpened():
        return []

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total_frames <= 0:
        frames = []
        while True:
            ret, frame = cap.read()
            if not ret or frame is None:
                break
            frames.append(frame)
        cap.release()
        total_frames = len(frames)
        if total_frames == 0:
            return []
        indices = [int(i * (total_frames - 1) / (count - 1)) if count > 1 else 0 for i in range(min(count, total_frames))]
        data_urls = []
        for idx in indices:
            d_url = frame_to_data_url(frames[idx])
            if d_url:
                data_urls.append(d_url)
        return data_urls

    indices = [int(i * (total_frames - 1) / (count - 1)) if count > 1 else 0 for i in range(min(count, total_frames))]
    data_urls = []
    for target_idx in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, target_idx)
        ret, frame = cap.read()
        if ret and frame is not None:
            d_url = frame_to_data_url(frame)
            if d_url:
                data_urls.append(d_url)

    cap.release()
    return data_urls


def analyze_visual_frames(
    frames: List[str],
    user_prompt: str = "Look at this",
    custom_system_prompt: Optional[str] = None,
    client: Optional[OpenAI] = None,
    model: Optional[str] = None,
) -> str:
    """Analyze a sequence of keyframes sampled from the camera/video clip with OpenAI Vision model."""
    if not frames:
        return "I wasn't able to receive any visual frames from the camera to look at."

    ai_client = client or _get_vision_client()
    if ai_client is None:
        return (
            f"I recorded the video clip with {len(frames)} frames, but the OpenAI API key is not configured "
            "for vision analysis. Set OPENAI_API_KEY to enable full live vision reasoning."
        )

    vision_model = model or _get_vision_model()

    system_instruction = custom_system_prompt or (
        "You are Future, an advanced autonomous AI assistant equipped with real-time computer vision. "
        "The user triggered vision capture by saying 'Look at this' (or a related camera request). "
        "You are provided with a sequence of keyframes sampled from a 5-second camera video clip. "
        "Thoroughly and accurately describe what you see:\n"
        "1. Identify all key objects, people, electronics, tools, screens, gestures, or surroundings.\n"
        "2. Read and transcribe any visible text, code, titles, errors, labels, or numbers clearly.\n"
        "3. Explain what the user is doing, pointing at, or showing you.\n"
        "4. Provide direct, helpful insights or answers relevant to what is shown, in a natural, confident tone.\n"
        "Keep your response concise yet thorough so this information is permanently useful in memory."
    )

    user_content: List[Dict[str, object]] = [
        {
            "type": "text",
            "text": (
                f"User request: {user_prompt.strip()}\n\n"
                f"Here are {len(frames)} sequential frames captured over the 5-second camera clip. "
                "Inspect the visual contents carefully and tell me what you see and how you can help:"
            ),
        }
    ]

    for frame_url in frames:
        if isinstance(frame_url, str) and frame_url.startswith("data:image/"):
            user_content.append({
                "type": "image_url",
                "image_url": {"url": frame_url, "detail": "auto"},
            })

    try:
        response = ai_client.chat.completions.create(
            model=vision_model,
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_content},
            ],
            max_tokens=800,
        )
        content = (response.choices[0].message.content or "").strip()
        return content or "I observed the video clip, but no details could be described."
    except Exception as exc:
        print(f"[Warning] OpenAI Vision API call failed: {exc}")
        return f"I captured the camera clip, but encountered an issue during vision analysis: {exc}"


def save_visual_memory(
    user_query: str,
    visual_analysis: str,
    video_path: Optional[str] = None,
    client_time: Optional[str] = None,
) -> None:
    """Store the visual observation into Future's long-term memory so it can be recalled and referenced later."""
    try:
        timestamp_str = client_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        user_entry = f"[Visual Camera Clip at {timestamp_str}]: {user_query.strip()}"
        ai_entry = (
            f"Visual observation & memory: {visual_analysis.strip()}"
            + (f" [Recorded video file: {video_path}]" if video_path else "")
        )

        memory = load_memory()
        remember(memory, user_entry, ai_entry)
        save_memory(memory)
    except Exception as exc:
        print(f"[Warning] Failed to persist visual memory: {exc}")


def look_at_this(
    user_query: str = "Look at this",
    duration: float = 5.0,
    cam_index: int = 0,
    provided_frames: Optional[List[str]] = None,
    client: Optional[OpenAI] = None,
    model: Optional[str] = None,
    client_time: Optional[str] = None,
) -> Dict[str, Union[bool, str, List[str], int, float]]:
    """Complete workflow for 'Look at this' command:
    
    1. If `provided_frames` is passed (e.g. from browser or mobile camera), uses them directly.
       Otherwise, captures a 5-second video from the laptop/system webcam using OpenCV.
    2. Runs multi-frame visual analysis with OpenAI Vision.
    3. Saves the detailed observation into long-term memory for immediate & future recall.
    4. Returns a comprehensive result payload.
    """
    frames = list(provided_frames or [])
    video_path = ""
    frame_count = len(frames)
    actual_duration = float(duration)

    if not frames:
        # Capture from local laptop / system camera
        capture_result = capture_video_clip(duration=duration, cam_index=cam_index, sample_frames=4)
        if capture_result.get("success"):
            frames = capture_result.get("frames", [])
            video_path = str(capture_result.get("video_path", ""))
            frame_count = int(capture_result.get("frame_count", 0))
            actual_duration = float(capture_result.get("duration", duration))
        else:
            err = str(capture_result.get("error", "Camera capture unavailable"))
            fallback_reply = (
                f"I tried to look through your camera, but couldn't open the video device ({err}). "
                "If you are on mobile or the web dashboard, use the camera button to stream directly."
            )
            return {
                "success": False,
                "reply": fallback_reply,
                "error": err,
                "frames": [],
                "video_path": "",
                "frame_count": 0,
                "duration": 0.0,
            }

    # Run AI Vision Analysis
    analysis_text = analyze_visual_frames(
        frames=frames,
        user_prompt=user_query,
        client=client,
        model=model,
    )

    # Save to Future's long-term memory
    save_visual_memory(
        user_query=user_query,
        visual_analysis=analysis_text,
        video_path=video_path,
        client_time=client_time,
    )

    return {
        "success": True,
        "reply": analysis_text,
        "summary": analysis_text,
        "frames": frames,
        "video_path": video_path,
        "frame_count": frame_count,
        "duration": actual_duration,
        "timestamp": datetime.now().isoformat(),
        "error": None,
    }


def get_vision_status() -> Dict[str, Union[bool, str, int, List[int]]]:
    """Check availability of OpenCV, camera devices, and Vision API configuration."""
    opencv_available = cv2 is not None
    available_cameras = []
    
    if opencv_available:
        for idx in range(4):
            try:
                test_cap = cv2.VideoCapture(idx)
                if test_cap.isOpened():
                    available_cameras.append(idx)
                    test_cap.release()
            except Exception:
                continue

    has_openai = _get_vision_client() is not None
    model_name = _get_vision_model()

    return {
        "opencv_available": opencv_available,
        "available_cameras": available_cameras,
        "camera_count": len(available_cameras),
        "openai_vision_configured": has_openai,
        "vision_model": model_name,
        "video_dir": str(get_video_dir()),
    }
