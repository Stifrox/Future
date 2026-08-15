e# vision.py
import cv2
import time
import os
import threading
from config import VIDEO_PATH, VIDEO_CHUNK_SECONDS
from memory import store_memory

os.makedirs(VIDEO_PATH, exist_ok=True)

def _record_one_chunk(filename, duration, cam_index=0, fps=15, res=(640,480)):
    cap = cv2.VideoCapture(cam_index)
    # Try to set resolution (best effort)
    try:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, res[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, res[1])
    except Exception:
        pass

    fourcc = cv2.VideoWriter_fourcc(*'XVID')
    out = cv2.VideoWriter(filename, fourcc, fps, res)
    start = time.time()
    while (time.time() - start) < duration:
        ret, frame = cap.read()
        if not ret:
            time.sleep(0.05)
            continue
        out.write(frame)
    out.release()
    cap.release()

def record_video_loop():
    """Runs forever; intended to be started in a background thread."""
    print("Video recorder started.")
    while True:
        ts = int(time.time())
        filename = os.path.join(VIDEO_PATH, f"log_{ts}.avi")
        try:
            _record_one_chunk(filename, duration=VIDEO_CHUNK_SECONDS)
            # notify memory manager that a video chunk exists
            store_memory("[Video log recorded]", filename)
        except Exception as e:
            print("Video capture error:", e)
            time.sleep(1)

def start_video_thread():
    t = threading.Thread(target=record_video_loop, daemon=True)
    t.start()
    return t
