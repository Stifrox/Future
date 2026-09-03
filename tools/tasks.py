"""Generic background task queue for long-running autonomous work.

Additive infrastructure so future features (Instagram content generation,
in-background self-updates, mini-agent deployment) can enqueue work without
blocking a request or touching existing endpoint logic. Handlers are
registered by task type; the worker thread just pulls jobs off the queue.
"""
import json
import queue
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Optional

STATE_PATH = Path("data") / "background_tasks.json"
_MAX_STORED_TASKS = 200

_lock = threading.Lock()
_tasks: Dict[str, dict] = {}
_queue: "queue.Queue[str]" = queue.Queue()
_handlers: Dict[str, Callable[[dict], object]] = {}
_worker_started = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _persist() -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            ordered = sorted(_tasks.values(), key=lambda t: t.get("created_at", ""))[-_MAX_STORED_TASKS:]
        STATE_PATH.write_text(json.dumps(ordered, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"Could not persist background task state: {exc}")


def register_handler(task_type: str, handler: Callable[[dict], object]) -> None:
    """Register a callable that will run for tasks of the given type."""
    _handlers[task_type] = handler


def _worker_loop() -> None:
    while True:
        task_id = _queue.get()
        with _lock:
            task = _tasks.get(task_id)
        if not task:
            continue

        handler = _handlers.get(task["type"])
        if not handler:
            with _lock:
                task["status"] = "error"
                task["error"] = f"No handler registered for task type '{task['type']}'"
                task["updated_at"] = _now_iso()
            _persist()
            continue

        with _lock:
            task["status"] = "running"
            task["progress_message"] = "Starting task"
            task["updated_at"] = _now_iso()
        _persist()

        try:
            payload = dict(task.get("payload", {}))
            payload["_task_id"] = task_id
            result = handler(payload)
            with _lock:
                task["status"] = "done"
                task["result"] = result
                task["updated_at"] = _now_iso()
        except Exception as exc:
            with _lock:
                task["status"] = "error"
                task["error"] = str(exc)
                task["updated_at"] = _now_iso()
        _persist()


def start_worker() -> None:
    """Start the single background worker thread. Safe to call multiple times."""
    global _worker_started
    with _lock:
        if _worker_started:
            return
        _worker_started = True
    threading.Thread(target=_worker_loop, daemon=True, name="future-task-worker").start()


def enqueue(task_type: str, payload: Optional[dict] = None) -> dict:
    """Queue a background task by type; returns the task's status record."""
    task_id = uuid.uuid4().hex
    task = {
        "id": task_id,
        "type": task_type,
        "payload": payload or {},
        "status": "queued",
        "progress_current": 0,
        "progress_total": 0,
        "progress_message": "Queued",
        "result": None,
        "error": None,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    with _lock:
        _tasks[task_id] = task
    _queue.put(task_id)
    _persist()
    start_worker()
    return task


def update_task(task_id: str, current: int = 0, total: int = 0, message: str = "") -> None:
    """Persist a short progress update for a task currently running in the worker."""
    with _lock:
        task = _tasks.get(task_id)
        if not task:
            return
        task["progress_current"] = max(0, int(current))
        task["progress_total"] = max(0, int(total))
        if message:
            task["progress_message"] = message
        task["updated_at"] = _now_iso()
    _persist()


def get_task(task_id: str) -> Optional[dict]:
    with _lock:
        return _tasks.get(task_id)


def list_tasks() -> list:
    with _lock:
        return sorted(_tasks.values(), key=lambda t: t.get("created_at", ""), reverse=True)
