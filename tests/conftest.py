import pytest

import updater


@pytest.fixture(autouse=True)
def isolate_self_update_state(tmp_path, monkeypatch):
    """Keep tests from re-applying the real saved self-update plan to the source tree."""
    monkeypatch.setattr(updater, "LAST_PLAN_PATH", tmp_path / "last_update_plan.json")
    monkeypatch.setattr(updater, "LOG_PATH", tmp_path / "update_requests.jsonl")
