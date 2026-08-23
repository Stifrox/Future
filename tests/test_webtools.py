from types import SimpleNamespace
from pathlib import Path

import webtools


def test_handle_query_uses_and_updates_long_term_memory(monkeypatch):
    captured = {}
    saved = {}

    def fake_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Your drone project is Skyhook."))]
        )

    monkeypatch.setattr(
        webtools,
        "_client",
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))),
    )
    monkeypatch.setattr(
        webtools,
        "load_personality",
        lambda: {"name": "Future", "traits": ["helpful"], "tone": "direct"},
    )
    monkeypatch.setattr(
        webtools,
        "load_memory",
        lambda: [{"user": "what is my drone project called", "ai": "Your drone project is Skyhook."}],
    )
    monkeypatch.setattr(webtools, "save_memory", lambda memory: saved.setdefault("memory", list(memory)))

    reply = webtools.handle_query("what is my drone project called?")

    assert reply == "Your drone project is Skyhook."
    assert "Skyhook" in captured["messages"][0]["content"]
    assert saved["memory"][-1] == {
        "user": "what is my drone project called?",
        "ai": "Your drone project is Skyhook.",
    }


def test_handle_query_stores_explicit_remember_request(monkeypatch):
    saved = {}

    monkeypatch.setattr(webtools, "load_memory", lambda: [])
    monkeypatch.setattr(webtools, "save_memory", lambda memory: saved.setdefault("memory", list(memory)))
    monkeypatch.setattr(webtools, "_client", None)

    reply = webtools.handle_query("can you remember the bathroom code at caribou is 3690")

    assert reply == "I'll remember that the bathroom code at caribou is 3690."
    assert saved["memory"][-1] == {
        "user": "can you remember the bathroom code at caribou is 3690",
        "ai": "I'll remember that the bathroom code at caribou is 3690.",
    }


def test_handle_query_recalls_project_name_from_saved_memory(monkeypatch):
    monkeypatch.setattr(
        webtools,
        "load_memory",
        lambda: [{"user": "my drone project is called project blackbird", "ai": "Noted."}],
    )
    monkeypatch.setattr(webtools, "_client", None)

    reply = webtools.handle_query("what is my drone project called")

    assert reply == "Your drone project is project blackbird."


def test_handle_query_memory_check_returns_deterministic_snapshot(monkeypatch):
    monkeypatch.setattr(
        webtools,
        "load_memory",
        lambda: [
            {"user": "my drone project is called project blackbird", "ai": "Noted."},
            {"user": "remember that my favorite color is blue", "ai": "I'll remember that your favorite color is blue."},
        ],
    )
    monkeypatch.setattr(webtools, "_client", None)

    reply = webtools.handle_query("check your memory and tell me if you see new stuff")

    assert "stored memory entries" in reply.lower()
    assert "most recent memory snippets" in reply.lower()
    assert "project blackbird" in reply.lower()


def test_handle_query_handles_printer_and_spotify_in_same_prompt(monkeypatch):
    monkeypatch.setattr(webtools, "_client", None)
    monkeypatch.setattr(webtools, "_handle_memory_intents", lambda query: None)
    monkeypatch.setattr(webtools, "_printer_status_reply", lambda: "Printer is currently idle.")
    monkeypatch.setattr(webtools, "handle_spotify_command", lambda query: "Queued and playing TiK ToK by Kesha.")

    reply = webtools.handle_query(
        "hey there can you check my 3d printer for anything on there as well as play some kesha on spotify"
    )

    assert "Printer is currently idle." in reply
    assert "Queued and playing TiK ToK by Kesha." in reply


def test_handle_query_creates_html_file_from_plain_command(monkeypatch, tmp_path):
    monkeypatch.setenv("FUTURE_FILES_ROOT", str(tmp_path))
    monkeypatch.setattr(webtools, "_client", None)

    reply = webtools.handle_query("create an html file called robot_status.html for robot status dashboard")

    created = tmp_path / "robot_status.html"
    assert created.exists()
    assert "Created robot_status.html" in reply
    contents = created.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in contents
    assert "robot status dashboard".lower() in contents.lower()


def test_handle_query_file_creation_respects_overwrite_guard(monkeypatch, tmp_path):
    monkeypatch.setenv("FUTURE_FILES_ROOT", str(tmp_path))
    monkeypatch.setattr(webtools, "_client", None)

    target = tmp_path / "notes.txt"
    target.write_text("existing", encoding="utf-8")

    reply = webtools.handle_query("create a text file named notes.txt")

    assert "already exists" in reply.lower()
    assert target.read_text(encoding="utf-8") == "existing"


def test_handle_query_routes_natural_today_schedule_phrase_to_calendar(monkeypatch):
    monkeypatch.setattr(webtools, "_client", None)
    monkeypatch.setattr(webtools, "handle_calendar_command", lambda query: "Here is your schedule today: 5:00 PM - Work")

    reply = webtools.handle_query("what do i have going on today")

    assert "schedule today" in reply.lower()


def test_handle_query_routes_natural_unread_messages_phrase_to_gmail(monkeypatch):
    monkeypatch.setattr(webtools, "_client", None)
    monkeypatch.setattr(webtools, "handle_gmail_command", lambda query: "Here are your latest Gmail messages: 1. From: A; Subject: B")

    reply = webtools.handle_query("do i have any unread messages")

    assert "latest gmail messages" in reply.lower()


def test_handle_query_combines_multiple_integration_intents(monkeypatch):
    monkeypatch.setattr(webtools, "_client", None)
    monkeypatch.setattr(webtools, "_printer_status_reply", lambda: "Printer is currently idle.")
    monkeypatch.setattr(webtools, "handle_calendar_command", lambda query: "Here is your schedule today: 5:00 PM - Work")

    reply = webtools.handle_query("is anything printing and what do i have going on today")

    assert "printer is currently idle" in reply.lower()
    assert "schedule today" in reply.lower()


def test_handle_query_rewrite_intent_returns_clarifier(monkeypatch):
    monkeypatch.setattr(webtools, "_client", None)

    reply = webtools.handle_query("i want to do a code rewrite for future")

    assert "what exactly are we upgrading" in reply.lower()
    assert "which model should power chat" in reply.lower()


def test_clean_model_text_unwraps_plain_fenced_conversation_text():
    raw = """```\nHey there, I can help with your schedule, inbox, or prints.\n```"""
    cleaned = webtools._clean_model_text(raw)
    assert cleaned == "Hey there, I can help with your schedule, inbox, or prints."


def test_clean_model_text_keeps_plain_class_word_as_prose_inside_fence():
    raw = """```\nclass but still very lost, please explain in depth\n```"""
    cleaned = webtools._clean_model_text(raw)
    assert cleaned == "class but still very lost, please explain in depth"


def test_response_length_profile_prefers_depth_when_requested():
    max_tokens, style = webtools._response_length_profile("please explain this in depth step by step")
    assert max_tokens >= 1200
    assert "depth" in style.lower()


def test_handle_query_routes_open_fusion_to_local_handler(monkeypatch):
    monkeypatch.setattr(webtools, "_client", None)
    monkeypatch.setattr(webtools, "_fusion_command_reply", lambda query: "Opening Fusion 360 now.")

    reply = webtools.handle_query("open fusion")

    assert "Opening Fusion 360 now." in reply


def test_handle_query_routes_open_named_fusion_file(monkeypatch):
    monkeypatch.setattr(webtools, "_client", None)
    monkeypatch.setattr(
        webtools,
        "_fusion_command_reply",
        lambda query: "Found and opened Fusion file 'prosthetic hand v12' from project 'Prosthetics'.",
    )

    reply = webtools.handle_query("open prosthetic hand in fusion")

    assert "Found and opened Fusion file" in reply


def test_handle_query_routes_pending_calendar_followup(monkeypatch):
    monkeypatch.setattr(webtools, "_client", None)
    monkeypatch.setattr(webtools, "has_pending_calendar_draft", lambda: True)
    monkeypatch.setattr(webtools, "should_handle_calendar_followup", lambda query: True)
    monkeypatch.setattr(webtools, "handle_calendar_command", lambda query: "Added to your calendar: Date with Tana.")

    reply = webtools.handle_query("12:00 PM tomorrow")

    assert "added to your calendar" in reply.lower()


def test_handle_query_routes_self_update_intent(monkeypatch):
    monkeypatch.setattr(webtools, "_client", None)
    monkeypatch.setattr(
        webtools,
        "self_update_plan",
        lambda instruction, target_files=None, scope="auto": {
            "status": "ok",
            "scope": "small_edit",
            "model": "claude-sonnet-4-5",
            "plan": {"summary": "Add docstring", "risk_level": "low"},
        },
    )

    reply = webtools.handle_query("run self update: small edit add docstring to api_server.py")

    assert "self-update plan ready" in reply.lower()
    assert "claude-sonnet-4-5" in reply


def test_handle_query_routes_self_update_execute_intent(monkeypatch):
    monkeypatch.setattr(webtools, "_client", None)
    monkeypatch.setenv("FUTURE_SELF_UPDATE_REQUIRE_VERIFICATION", "0")
    monkeypatch.setattr(
        webtools,
        "self_update_execute_latest",
        lambda: {"status": "ok", "applied": [{"path": "api_server.py", "operation": "insert_line"}]},
    )

    reply = webtools.handle_query("okay execute the update and lmk when its finished")

    assert "executed successfully" in reply.lower()


def test_handle_query_execute_requires_verification_then_executes(monkeypatch):
    monkeypatch.setattr(webtools, "_client", None)
    monkeypatch.setenv("FUTURE_SELF_UPDATE_REQUIRE_VERIFICATION", "1")
    monkeypatch.setattr(webtools, "_start_self_update_verification", lambda: "Verification required before execution. Reply with: verify update 123456")
    monkeypatch.setattr(
        webtools,
        "_PENDING_SELF_UPDATE_VERIFICATION",
        {"code": "123456", "expires_at": 9999999999},
    )
    monkeypatch.setattr(
        webtools,
        "self_update_execute_latest",
        lambda: {"status": "ok", "applied": [{"path": "api_server.py", "operation": "insert_line"}]},
    )

    first = webtools.handle_query("execute the update")
    second = webtools.handle_query("verify update 123456")

    assert "verification required" in first.lower()
    assert "executed successfully" in second.lower()


def test_handle_query_routes_okay_run_it_to_execute(monkeypatch):
    monkeypatch.setattr(webtools, "_client", None)
    monkeypatch.setattr(
        webtools,
        "self_update_execute_latest",
        lambda: {"status": "ok", "applied": [{"path": "webtools.py", "operation": "insert_line"}]},
    )

    reply = webtools.handle_query("okay run it")

    assert "executed successfully" in reply.lower()


def test_handle_query_runs_basic_code_rerun(monkeypatch, tmp_path):
    monkeypatch.setattr(webtools, "_client", None)
    monkeypatch.setattr(webtools, "_workspace_root", lambda: tmp_path)

    for name in ["main.py", "webtools.py", "api_server.py", "tools/integrations.py"]:
        file_path = tmp_path / name
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("print('ok')\n", encoding="utf-8")

    reply = webtools.handle_query("run a basic code rerun")

    assert "basic code rerun passed" in reply.lower()


def test_handle_query_explains_code_update_blocker(monkeypatch):
    monkeypatch.setattr(webtools, "_client", None)

    reply = webtools.handle_query("what exactly is preventing your ability to do code updates")

    assert "wrapper" in reply.lower() or "tool path" in reply.lower()


def test_handle_query_routes_plain_update_to_wrapper(monkeypatch):
    monkeypatch.setattr(webtools, "_client", None)

    reply = webtools.handle_query("update")

    assert "tell me what to update" in reply.lower()


def test_handle_query_auto_executes_small_edit_update(monkeypatch):
    monkeypatch.setattr(webtools, "_client", None)
    monkeypatch.setattr(
        webtools,
        "self_update_plan",
        lambda instruction, target_files=None, scope="auto": {
            "status": "ok",
            "scope": "small_edit",
            "model": "claude-sonnet-4-5",
            "plan": {"summary": "Add missing logging", "risk_level": "low"},
        },
    )
    monkeypatch.setattr(
        webtools,
        "self_update_execute_latest",
        lambda: {
            "status": "ok",
            "applied": [{"path": "webtools.py", "operation": "insert_line"}],
            "skipped": [],
        },
    )

    reply = webtools.handle_query("update my code")

    assert "executed successfully" in reply.lower()
    assert "plan ready" not in reply.lower()


def test_self_update_uses_default_target_files_when_none_present(monkeypatch):
    monkeypatch.setattr(webtools, "_client", None)

    captured = {}

    def fake_plan(instruction, target_files=None, scope="auto"):
        captured["target_files"] = list(target_files or [])
        return {
            "status": "ok",
            "scope": "small_edit",
            "model": "claude-sonnet-4-5",
            "plan": {"summary": "ok", "risk_level": "low"},
        }

    monkeypatch.setattr(webtools, "self_update_plan", fake_plan)

    webtools.handle_query("run self update to switch between male and female voice")

    assert "webtools.py" in captured["target_files"]
    assert "api_server.py" in captured["target_files"]


def test_resolve_fusion_executable_discovers_webdeploy_binary(monkeypatch, tmp_path):
    root = tmp_path / "webdeploy" / "production"
    exe = root / "abc123" / "Fusion360.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("", encoding="utf-8")

    monkeypatch.delenv("FUSION360_PATH", raising=False)
    monkeypatch.setenv("FUSION360_WEBDEPLOY_ROOT", str(root))

    resolved = webtools._resolve_fusion_executable()

    assert resolved is not None
    assert resolved.lower().endswith("fusion360.exe")