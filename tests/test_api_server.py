from fastapi.testclient import TestClient
import urllib.parse

import api_server


client = TestClient(api_server.app)


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_system_stats_has_expected_keys():
    response = client.get("/api/system/stats")
    payload = response.json()
    assert response.status_code == 200
    assert {"cpu", "ram", "gpu", "net_mbps"}.issubset(payload.keys())


def test_spotify_control_rejects_unknown_action():
    response = client.post("/api/spotify/control", json={"action": "invalid"})
    assert response.status_code == 400


def test_chat_message_returns_reply(monkeypatch):
    api_server.CHAT_CONTEXT_LINES.clear()
    monkeypatch.setattr(api_server, "_chat_reply", lambda message, recent_context=None: f"echo:{message}")
    response = client.post("/api/chat/message", json={"message": "hello"})
    assert response.status_code == 200
    assert response.json()["reply"] == "echo:hello"


def test_chat_message_passes_recent_context(monkeypatch):
    api_server.CHAT_CONTEXT_LINES.clear()
    api_server.CHAT_CONTEXT_LINES.append({"role": "user", "content": "first"})
    api_server.CHAT_CONTEXT_LINES.append({"role": "assistant", "content": "reply1"})

    captured = {}

    def fake_chat_reply(message, recent_context=None):
        captured["message"] = message
        captured["recent_context"] = list(recent_context or [])
        return "ok"

    monkeypatch.setattr(api_server, "_chat_reply", fake_chat_reply)

    response = client.post("/api/chat/message", json={"message": "second"})

    assert response.status_code == 200
    assert captured["message"] == "second"
    assert captured["recent_context"][0]["content"] == "first"
    assert captured["recent_context"][1]["content"] == "reply1"
    assert api_server.CHAT_CONTEXT_LINES[-2]["content"] == "second"
    assert api_server.CHAT_CONTEXT_LINES[-1]["content"] == "ok"


def test_anycubic_slice_endpoint(monkeypatch):
    monkeypatch.setattr(
        api_server,
        "slice_model",
        lambda input_path, output_path=None, profile_path=None: {
            "status": "sliced",
            "input_model": input_path,
            "output_gcode": output_path or "demo.gcode",
        },
    )
    response = client.post(
        "/api/anycubic/slice",
        json={"input_path": "C:/models/bracket.stl", "output_path": "C:/prints/bracket.gcode"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "sliced"
    assert payload["output_gcode"] == "C:/prints/bracket.gcode"


def test_anycubic_upload_endpoint(monkeypatch):
    monkeypatch.setattr(
        api_server,
        "upload_gcode",
        lambda gcode_path, start_print=False: {
            "uploaded": True,
            "started": start_print,
            "file": gcode_path,
        },
    )
    response = client.post(
        "/api/anycubic/upload",
        json={"gcode_path": "C:/prints/bracket.gcode", "start_print": True},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["uploaded"] is True
    assert payload["started"] is True


def test_files_write_creates_file(tmp_path, monkeypatch):
    monkeypatch.setenv("FUTURE_FILES_ROOT", str(tmp_path))

    response = client.post(
        "/api/files/write",
        json={"path": "generated/sample.html", "content": "<h1>Hello</h1>", "overwrite": False},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["overwritten"] is False
    assert (tmp_path / "generated" / "sample.html").read_text(encoding="utf-8") == "<h1>Hello</h1>"


def test_files_write_rejects_existing_file_without_overwrite(tmp_path, monkeypatch):
    monkeypatch.setenv("FUTURE_FILES_ROOT", str(tmp_path))
    target = tmp_path / "notes.txt"
    target.write_text("old", encoding="utf-8")

    response = client.post(
        "/api/files/write",
        json={"path": "notes.txt", "content": "new", "overwrite": False},
    )

    assert response.status_code == 409
    assert target.read_text(encoding="utf-8") == "old"


def test_connect_fusion360_returns_authorize_url(monkeypatch):
    monkeypatch.setenv("AUTODESK_CLIENT_ID", "demo-id")
    monkeypatch.setenv("AUTODESK_CLIENT_SECRET", "demo-secret")
    monkeypatch.setenv("AUTODESK_REDIRECT_URI", "http://localhost:8000/callback/autodesk")
    monkeypatch.setenv("AUTODESK_SCOPES", "data:read data:write")

    response = client.post("/api/connect/fusion360")

    assert response.status_code == 200
    payload = response.json()
    assert "auth_url" in payload
    assert payload["state"]
    parsed = urllib.parse.urlparse(payload["auth_url"])
    query = urllib.parse.parse_qs(parsed.query)
    assert query.get("client_id", [""])[0] == "demo-id"
    assert query.get("redirect_uri", [""])[0] == "http://localhost:8000/callback/autodesk"


def test_fusion_oauth_status_reports_disconnected(monkeypatch):
    monkeypatch.setattr(api_server, "_load_autodesk_tokens", lambda: {})

    response = client.get("/api/fusion360/oauth/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["connected"] is False
    assert payload["has_refresh_token"] is False


def test_fusion_open_file_returns_found_item(monkeypatch):
    monkeypatch.setattr(api_server, "_autodesk_access_token", lambda: "token")
    monkeypatch.setattr(
        api_server,
        "_autodesk_fetch_projects",
        lambda token, max_hubs=4, max_projects_per_hub=20: [
            {
                "hub_id": "hub1",
                "hub_name": "Main Hub",
                "project_id": "proj1",
                "project_name": "Prosthetics",
                "updated_at": "2026-08-01T00:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        api_server,
        "_autodesk_search_project_items",
        lambda token, hub_id, project_id, query: [
            {
                "name": "prosthetic hand v12",
                "item_id": "urn:adsk.wipprod:dm.lineage:abc",
                "project_id": project_id,
                "hub_id": hub_id,
                "web_url": "https://example.autodesk/view/file",
                "version_urn": "urn:adsk.wipprod:fs.file:vf.xyz",
            }
        ],
    )
    monkeypatch.setattr(api_server.webbrowser, "open", lambda url: True)

    response = client.post("/api/fusion360/open", json={"file_name": "prosthetic hand", "open_in_browser": True})

    assert response.status_code == 200
    payload = response.json()
    assert payload["found"] is True
    assert payload["opened"] is True
    assert payload["file"]["name"] == "prosthetic hand v12"


def test_self_update_plan_endpoint_returns_plan(monkeypatch):
    monkeypatch.setattr(
        api_server,
        "self_update_plan",
        lambda instruction, target_files=None, scope="auto": {
            "status": "ok",
            "scope": scope,
            "model": "claude-sonnet-4-5",
            "raw_response": "{}",
            "plan": {"summary": "ok"},
        },
    )

    response = client.post(
        "/api/self-update/plan",
        json={
            "instruction": "Update one function",
            "target_files": ["webtools.py"],
            "scope": "small_edit",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["model"] == "claude-sonnet-4-5"


def test_self_update_execute_endpoint_returns_applied_edits(monkeypatch):
    monkeypatch.setattr(
        api_server,
        "self_update_execute_latest",
        lambda: {"status": "ok", "applied": [{"path": "api_server.py", "operation": "insert_line"}]},
    )

    response = client.post("/api/self-update/execute")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["applied"][0]["path"] == "api_server.py"


def test_elevenlabs_tts_endpoint_returns_audio(monkeypatch):
    monkeypatch.setattr(api_server, "_synthesize_elevenlabs_audio", lambda text: b"FAKEAUDIO")

    response = client.post("/api/tts/elevenlabs", json={"text": "Hello from Future"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/mpeg")
    assert response.content == b"FAKEAUDIO"


def test_elevenlabs_tts_endpoint_trims_long_text_to_intro(monkeypatch):
    captured = {}

    def fake_synthesize(text):
        captured["text"] = text
        return b"FAKEAUDIO"

    monkeypatch.setattr(api_server, "_synthesize_elevenlabs_audio", fake_synthesize)

    long_text = (
        "Absolutely. Here is a very long response with several sections and details.\n"
        "Step 1: Audit your modules and list technical debt.\n"
        "Step 2: Decide migration boundaries and rollout strategy.\n\n"
        "Step 3: Build and verify each stage with tests and benchmarks.\n"
        "Step 4: Finalize deployment and monitor regressions carefully."
    )

    response = client.post("/api/tts/elevenlabs", json={"text": long_text})

    assert response.status_code == 200
    assert "step 3" not in captured["text"].lower()
    assert "absolutely" in captured["text"].lower()


def test_elevenlabs_tts_endpoint_keeps_short_text(monkeypatch):
    captured = {}

    def fake_synthesize(text):
        captured["text"] = text
        return b"FAKEAUDIO"

    monkeypatch.setattr(api_server, "_synthesize_elevenlabs_audio", fake_synthesize)

    response = client.post("/api/tts/elevenlabs", json={"text": "Quick check from Future."})

    assert response.status_code == 200
    assert captured["text"] == "Quick check from Future."


def test_elevenlabs_tts_endpoint_rejects_empty_text():
    response = client.post("/api/tts/elevenlabs", json={"text": "   "})

    assert response.status_code == 400
