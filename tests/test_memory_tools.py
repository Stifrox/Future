import json
from pathlib import Path

from tools import memory


def test_load_memory_merges_legacy_and_primary_files(monkeypatch, tmp_path):
    legacy_path = tmp_path / "data_future_memory.json"
    long_term_path = tmp_path / "long_term_memory.json"
    primary_path = tmp_path / "future_memory.json"

    legacy_path.write_text(
        json.dumps(
            [
                {"speaker": "user", "text": "my drone project is called project blackbird"},
                {"speaker": "future", "text": "Noted."},
            ]
        ),
        encoding="utf-8",
    )
    long_term_path.write_text(
        json.dumps(
            [
                {"user": "can you remember the bathroom code at caribou is 3690", "ai": "I'll remember that the bathroom code at caribou is 3690."}
            ]
        ),
        encoding="utf-8",
    )
    primary_path.write_text(
        json.dumps(
            [
                {"user": "what is my drone project called", "ai": "Your drone project is project blackbird."}
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(memory, "_memory_paths", lambda: [legacy_path, long_term_path, primary_path])

    merged = memory.load_memory()

    assert {"user": "my drone project is called project blackbird", "ai": "Noted."} in merged
    assert {"user": "can you remember the bathroom code at caribou is 3690", "ai": "I'll remember that the bathroom code at caribou is 3690."} in merged
    assert {"user": "what is my drone project called", "ai": "Your drone project is project blackbird."} in merged