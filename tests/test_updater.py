import updater


def test_choose_scope_prefers_full_rewrite_for_rewrite_instruction():
    scope = updater._choose_scope("Please do a full rewrite of the module", [], "auto")
    assert scope == "full_rewrite"


def test_choose_scope_defaults_to_small_edit_for_simple_instruction():
    scope = updater._choose_scope("Rename one function", ["webtools.py"], "auto")
    assert scope == "small_edit"


def test_choose_model_uses_tiered_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MODEL_SMALL_EDIT", "claude-small")
    monkeypatch.setenv("ANTHROPIC_MODEL_FULL_REWRITE", "claude-large")

    assert updater._choose_model("small_edit") == "claude-small"
    assert updater._choose_model("full_rewrite") == "claude-large"


def test_self_update_plan_returns_error_without_anthropic_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    payload = updater.self_update_plan(
        instruction="Refactor helper",
        target_files=["webtools.py"],
        scope="small_edit",
    )

    assert payload["status"] == "error"
    assert "ANTHROPIC_API_KEY" in payload["error"]