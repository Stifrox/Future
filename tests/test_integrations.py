import os
import main
import tools.integrations as integrations
from tools.integrations import build_google_calendar_event_url, build_spotify_url, build_spotify_deeplink


def test_build_spotify_url_uses_search_query():
    url = build_spotify_url("chill beats")
    assert url.startswith("https://open.spotify.com/search/")
    assert "chill%20beats" in url


def test_build_google_calendar_event_url_contains_title_and_template_action():
    url = build_google_calendar_event_url("Dentist", "Checkup", "2026-06-26 14:00")
    assert "action=TEMPLATE" in url
    assert "Dentist" in url
    assert "Checkup" in url


def test_build_spotify_deeplink_uses_spotify_search_uri():
    uri = build_spotify_deeplink("chill beats")
    assert uri.startswith("spotify:search:")
    assert "chill%20beats" in uri


def test_get_spotify_auth_url_contains_redirect_uri_and_scope():
    integrations.SPOTIFY_CLIENT_ID = "demo-client"
    integrations.SPOTIFY_REDIRECT_URI = "http://localhost:8888/callback"
    url = integrations.get_spotify_auth_url()
    assert "client_id=demo-client" in url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8888%2Fcallback" in url
    assert "scope=user-read-playback-state" in url


def test_google_calendar_auth_url_uses_configured_client_id():
    integrations.GOOGLE_CALENDAR_CLIENT_ID = "demo-google-client"
    integrations.GOOGLE_CALENDAR_REDIRECT_URI = "http://localhost:8000/callback"
    url = integrations.get_google_calendar_auth_url()
    assert "client_id=demo-google-client" in url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8000%2Fcallback" in url


def test_google_auth_url_accepts_custom_scopes():
    integrations.GOOGLE_CALENDAR_CLIENT_ID = "demo-google-client"
    integrations.GOOGLE_CALENDAR_REDIRECT_URI = "http://localhost:8000/callback"
    url = integrations.get_google_auth_url(scopes=["scope.one", "scope.two"])
    assert "scope=scope.one+scope.two" in url


def test_google_calendar_credentials_are_loaded_from_environment(monkeypatch):
    import importlib

    monkeypatch.setenv("GOOGLE_CALENDAR_CLIENT_ID", "demo-google-client")
    monkeypatch.setenv("GOOGLE_CALENDAR_CLIENT_SECRET", "demo-google-secret")

    module = importlib.reload(integrations)

    assert module.GOOGLE_CALENDAR_CLIENT_ID == "demo-google-client"
    assert module.GOOGLE_CALENDAR_CLIENT_SECRET == "demo-google-secret"


def test_load_environment_file_populates_google_calendar_credentials(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("GOOGLE_CALENDAR_CLIENT_ID=dotenv-client\nGOOGLE_CALENDAR_CLIENT_SECRET=dotenv-secret\n", encoding="utf-8")

    monkeypatch.delenv("GOOGLE_CALENDAR_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CALENDAR_CLIENT_SECRET", raising=False)

    module = integrations
    loaded = module.load_environment_file(env_file)

    assert loaded is True
    assert os.environ["GOOGLE_CALENDAR_CLIENT_ID"] == "dotenv-client"
    assert os.environ["GOOGLE_CALENDAR_CLIENT_SECRET"] == "dotenv-secret"


def test_list_google_calendar_events_supports_month_range(monkeypatch):
    captured = {}

    class FixedDateTime(integrations.datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 16, 12, 0, 0, tzinfo=tz)

    class DummyResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "items": [
                    {"summary": "Planning", "start": {"dateTime": "2026-08-16T09:00:00Z"}},
                ]
            }

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return DummyResponse()

    monkeypatch.setattr(integrations.datetime, "datetime", FixedDateTime)
    monkeypatch.setattr(integrations, "GOOGLE_CALENDAR_ACCESS_TOKEN", "access")
    monkeypatch.setattr(integrations, "GOOGLE_CALENDAR_REFRESH_TOKEN", None)
    monkeypatch.setattr(integrations.requests, "get", fake_get)

    events = integrations.list_google_calendar_events(range_name="month", max_results=50)

    assert captured["params"]["timeMin"].startswith("2026-08-01")
    assert captured["params"]["timeMax"].startswith("2026-09-01")
    assert events[0]["date"] == "2026-08-16"
    assert events[0]["time"] == "9:00 AM"


def test_validate_spotify_credentials_uses_client_credentials(monkeypatch):
    class DummyResponse:
        def __init__(self):
            self.status_code = 200
        def raise_for_status(self):
            return None
        def json(self):
            return {"access_token": "abc123"}

    def fake_post(url, data=None, auth=None, timeout=None):
        assert url == "https://accounts.spotify.com/api/token"
        assert data == {"grant_type": "client_credentials"}
        assert auth == ("demo-client", "demo-secret")
        assert timeout == 20
        return DummyResponse()

    monkeypatch.setattr(integrations.requests, "post", fake_post)
    monkeypatch.setattr(integrations, "SPOTIFY_CLIENT_ID", "demo-client")
    monkeypatch.setattr(integrations, "SPOTIFY_CLIENT_SECRET", "demo-secret")

    result = integrations.validate_spotify_credentials()
    assert result["access_token"] == "abc123"


def test_handle_spotify_command_routes_pause_and_volume(monkeypatch):
    called = []

    def fake_pause():
        called.append("pause")

    def fake_set_volume(value):
        called.append(("volume", value))

    monkeypatch.setattr(integrations, "spotify_pause", fake_pause)
    monkeypatch.setattr(integrations, "spotify_set_volume", fake_set_volume)

    monkeypatch.setattr(integrations, "SPOTIFY_ACCESS_TOKEN", "demo-token")

    reply = integrations.handle_spotify_command("pause spotify")
    assert reply == "Pausing Spotify."
    assert called == ["pause"]

    reply = integrations.handle_spotify_command("volume up")
    assert reply == "Setting Spotify volume to 80 percent."
    assert called[-1] == ("volume", 80)


def test_handle_spotify_command_routes_unpause_to_play(monkeypatch):
    called = []

    monkeypatch.setattr(integrations, "load_spotify_tokens_into_globals", lambda: ("demo-token", "demo-refresh"))
    monkeypatch.setattr(integrations, "_ensure_spotify_user_token", lambda force_refresh=False: True)
    monkeypatch.setattr(integrations, "spotify_play", lambda uri=None: called.append(("play", uri)) or True)

    reply = integrations.handle_spotify_command("unpause")

    assert reply == "Resuming Spotify."
    assert called == [("play", None)]


def test_handle_spotify_command_reports_no_active_device_without_reauth(monkeypatch):
    class DummyResponse:
        status_code = 404
        text = ""

        def json(self):
            return {"error": {"reason": "NO_ACTIVE_DEVICE", "message": "No active device found"}}

    monkeypatch.setattr(integrations, "load_spotify_tokens_into_globals", lambda: ("demo-token", "demo-refresh"))
    monkeypatch.setattr(integrations, "_ensure_spotify_user_token", lambda force_refresh=False: True)

    def fake_pause():
        raise integrations.requests.HTTPError("No active device", response=DummyResponse())

    monkeypatch.setattr(integrations, "spotify_pause", fake_pause)
    monkeypatch.setattr(integrations, "request_spotify_authorization", lambda reason="": "should not auth")

    reply = integrations.handle_spotify_command("pause")

    assert "no active playback device" in reply.lower()


def test_handle_spotify_command_requests_auth_when_unavailable(monkeypatch):
    opened = []

    monkeypatch.setattr(integrations, "load_spotify_tokens_into_globals", lambda: (None, None))
    monkeypatch.setattr(integrations, "SPOTIFY_ACCESS_TOKEN", None)
    monkeypatch.setattr(integrations, "SPOTIFY_REFRESH_TOKEN", None)
    monkeypatch.setattr(integrations.webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(integrations, "get_spotify_auth_url", lambda: "https://example.com/auth")

    reply = integrations.handle_spotify_command("pause")

    assert "authorization page" in reply.lower()
    assert opened == ["https://example.com/auth"]


def test_handle_spotify_command_queues_and_plays_top_result(monkeypatch):
    called = []

    monkeypatch.setattr(integrations, "load_spotify_tokens_into_globals", lambda: ("demo-token", None))
    monkeypatch.setattr(integrations, "SPOTIFY_ACCESS_TOKEN", "demo-token")
    monkeypatch.setattr(
        integrations,
        "spotify_search",
        lambda query: {
            "tracks": {
                "items": [
                    {
                        "uri": "spotify:track:abc123",
                        "name": "Test Song",
                        "artists": [{"name": "Test Artist"}],
                        "external_urls": {"spotify": "https://open.spotify.com/track/abc123"},
                    }
                ]
            }
        },
    )
    monkeypatch.setattr(integrations, "spotify_add_to_queue", lambda uri: called.append(("queue", uri)) or True)
    monkeypatch.setattr(integrations, "spotify_play", lambda uri=None: called.append(("play", uri)) or True)

    reply = integrations.handle_spotify_command("play test song")

    assert "queued and playing" in reply.lower()
    assert called[0] == ("queue", "spotify:track:abc123")
    assert called[1] == ("play", None)


def test_handle_spotify_command_without_token_opens_top_track(monkeypatch):
    monkeypatch.setattr(integrations, "load_spotify_tokens_into_globals", lambda: (None, None))
    monkeypatch.setattr(integrations, "SPOTIFY_ACCESS_TOKEN", None)
    monkeypatch.setattr(integrations, "SPOTIFY_REFRESH_TOKEN", None)
    monkeypatch.setattr(
        integrations,
        "spotify_search",
        lambda query: {
            "tracks": {
                "items": [
                    {
                        "uri": "spotify:track:def456",
                        "name": "Another Song",
                        "artists": [{"name": "Another Artist"}],
                        "external_urls": {"spotify": "https://open.spotify.com/track/def456"},
                    }
                ]
            }
        },
    )
    monkeypatch.setattr(integrations, "request_spotify_authorization", lambda reason="": f"auth needed: {reason}")

    reply = integrations.handle_spotify_command("play another song")

    assert "auth needed" in reply.lower()


def test_prepare_spotify_query_cleans_on_spotify_phrase():
    query = integrations._prepare_spotify_query("play feel it on spotify")
    assert query == "feel it"


def test_prepare_spotify_query_extracts_music_clause_from_mixed_prompt():
    query = integrations._prepare_spotify_query(
        "hey there can you check my 3d printer for anything on there as well as play some kesha on spotify"
    )
    assert query == "kesha"


def test_handle_calendar_command_creates_event(monkeypatch):
    calls = []

    def fake_create_event(title, details="", start_time=None, end_time=None, calendar_id="primary"):
        calls.append((title, details, start_time, end_time, calendar_id))
        return {"id": "evt-123"}

    monkeypatch.setattr(integrations, "create_google_calendar_event", fake_create_event)

    reply = integrations.handle_calendar_command("schedule dentist appointment tomorrow at 3pm")

    assert "dentist appointment" in reply.lower()
    assert calls


def test_parse_calendar_time_gym_today_7pm():
    start, end = integrations.parse_calendar_time("gym at 7pm today")
    assert start is not None
    assert end is not None
    assert start.endswith("+00:00") or start.endswith("-00:00") or start.endswith("Z") or "+" in start or "-" in start
    assert end != start


def test_handle_calendar_command_parses_gym_at_7pm_today(monkeypatch):
    calls = []

    def fake_create_event(title, details="", start_time=None, end_time=None, calendar_id="primary"):
        calls.append((title, details, start_time, end_time, calendar_id))
        return {"id": "evt-456"}

    monkeypatch.setattr(integrations, "create_google_calendar_event", fake_create_event)

    reply = integrations.handle_calendar_command("schedule gym at 7pm today")

    assert "gym" in reply.lower()
    assert calls
    assert calls[0][0].lower() == "gym"


def test_handle_calendar_command_accepts_plan_keyword(monkeypatch):
    calls = []

    def fake_create_event(title, details="", start_time=None, end_time=None, calendar_id="primary"):
        calls.append((title, details, start_time, end_time, calendar_id))
        return {"id": "evt-plan-1"}

    monkeypatch.setattr(integrations, "create_google_calendar_event", fake_create_event)

    reply = integrations.handle_calendar_command("plan dentist appointment tomorrow at 3pm")

    assert "added to your calendar" in reply.lower()
    assert calls


def test_handle_calendar_command_without_time_asks_for_time(monkeypatch):
    monkeypatch.setattr(integrations, "load_google_calendar_tokens_into_globals", lambda: ("access", "refresh"))
    monkeypatch.setattr(integrations, "GOOGLE_CALENDAR_ACCESS_TOKEN", "access")
    monkeypatch.setattr(integrations, "GOOGLE_CALENDAR_REFRESH_TOKEN", "refresh")

    reply = integrations.handle_calendar_command("schedule dentist tomorrow")

    assert "need a time" in reply.lower()


def test_handle_calendar_command_extracts_clean_reminder_title(monkeypatch):
    seen = {}

    def fake_create_event(title, details="", start_time=None, end_time=None, calendar_id="primary"):
        seen["title"] = title
        return {"id": "evt-clean-title"}

    monkeypatch.setattr(integrations, "create_google_calendar_event", fake_create_event)

    reply = integrations.handle_calendar_command("can you set a reminder to get to work at five today?")

    assert "added to your calendar" in reply.lower()
    assert seen["title"].lower() == "get to work"


def test_handle_calendar_command_lists_events_for_today(monkeypatch):
    monkeypatch.setattr(
        integrations,
        "list_google_calendar_events",
        lambda range_name="today", max_results=8, calendar_id="primary": [
            {"time": "5:00 PM", "title": "Work", "subtitle": ""},
            {"time": "7:00 PM", "title": "Gym", "subtitle": "Upper body"},
        ],
    )

    reply = integrations.handle_calendar_command("what do i have going on today")

    assert "schedule today" in reply.lower()
    assert "work" in reply.lower()
    assert "gym" in reply.lower()


def test_handle_calendar_command_reports_empty_schedule(monkeypatch):
    monkeypatch.setattr(integrations, "list_google_calendar_events", lambda range_name="today", max_results=8, calendar_id="primary": [])

    reply = integrations.handle_calendar_command("what's on my calendar today")

    assert "nothing scheduled today" in reply.lower()


def test_handle_calendar_command_lookup_accepts_calender_typo(monkeypatch):
    monkeypatch.setattr(
        integrations,
        "list_google_calendar_events",
        lambda range_name="today", max_results=8, calendar_id="primary": [
            {"time": "12:00 PM", "title": "Gym", "subtitle": ""},
        ],
    )

    reply = integrations.handle_calendar_command("nah can you check my calender for me")

    assert "schedule today" in reply.lower()
    assert "gym" in reply.lower()


def test_handle_calendar_command_tmrw_alias_sets_tomorrow_date(monkeypatch):
    captured = {}

    def fake_create_event(title, details="", start_time=None, end_time=None, calendar_id="primary"):
        captured["start_time"] = start_time
        return {
            "summary": title,
            "start": {"dateTime": start_time},
            "end": {"dateTime": end_time},
        }

    monkeypatch.setattr(integrations, "create_google_calendar_event", fake_create_event)

    reply = integrations.handle_calendar_command("schedule gym tmrw at 12 pm")

    expected_date = (integrations.datetime.datetime.now() + integrations.datetime.timedelta(days=1)).date().isoformat()
    assert captured["start_time"].startswith(expected_date)
    assert "added to your calendar" in reply.lower()
    assert "gym" in reply.lower()


def test_handle_calendar_command_confirms_created_event_details(monkeypatch):
    def fake_create_event(title, details="", start_time=None, end_time=None, calendar_id="primary"):
        return {
            "summary": title,
            "start": {"dateTime": start_time},
            "end": {"dateTime": end_time},
            "location": "Downtown Gym",
        }

    monkeypatch.setattr(integrations, "create_google_calendar_event", fake_create_event)

    reply = integrations.handle_calendar_command("schedule gym tomorrow at 12 pm")

    assert "added to your calendar" in reply.lower()
    assert "scheduled for" in reply.lower()
    assert "location: downtown gym" in reply.lower()


def test_handle_calendar_command_accepts_time_only_followup(monkeypatch):
    calls = []

    def fake_create_event(title, details="", start_time=None, end_time=None, calendar_id="primary"):
        calls.append((title, start_time, end_time))
        return {
            "summary": title,
            "start": {"dateTime": start_time},
            "end": {"dateTime": end_time},
        }

    monkeypatch.setattr(integrations, "create_google_calendar_event", fake_create_event)
    integrations.PENDING_CALENDAR_DRAFT = None

    first = integrations.handle_calendar_command("schedule meeting tomorrow")
    second = integrations.handle_calendar_command("12pm")

    expected_date = (integrations.datetime.datetime.now() + integrations.datetime.timedelta(days=1)).date().isoformat()
    assert "still need a time" in first.lower()
    assert calls
    assert calls[0][0].lower() == "meeting"
    assert calls[0][1].startswith(expected_date)
    assert "added to your calendar" in second.lower()


def test_handle_google_calendar_callback_stores_tokens(monkeypatch):
    captured = {}

    def fake_exchange(code):
        assert code == "demo-code"
        return {"access_token": "abc123", "refresh_token": "xyz789"}

    def fake_set_tokens(access_token, refresh_token=None):
        captured["access_token"] = access_token
        captured["refresh_token"] = refresh_token

    monkeypatch.setattr(integrations, "exchange_google_calendar_code", fake_exchange)
    monkeypatch.setattr(integrations, "set_google_calendar_tokens", fake_set_tokens)

    response = integrations.handle_google_calendar_callback({"code": "demo-code"})

    assert response == "Google Calendar connected successfully."
    assert captured == {"access_token": "abc123", "refresh_token": "xyz789"}


def test_complete_google_calendar_authorization_stores_tokens(monkeypatch):
    captured = {}

    def fake_exchange(code):
        assert code == "demo-code"
        return {"access_token": "abc123", "refresh_token": "xyz789"}

    def fake_set_tokens(access_token, refresh_token=None):
        captured["access_token"] = access_token
        captured["refresh_token"] = refresh_token

    monkeypatch.setattr(integrations, "exchange_google_calendar_code", fake_exchange)
    monkeypatch.setattr(integrations, "set_google_calendar_tokens", fake_set_tokens)

    response = integrations.complete_google_calendar_authorization("demo-code")

    assert response == "Google Calendar connected successfully."
    assert captured == {"access_token": "abc123", "refresh_token": "xyz789"}


def test_create_google_calendar_event_loads_saved_tokens_before_request(monkeypatch):
    calls = []

    class DummyResponse:
        status_code = 200
        def raise_for_status(self):
            return None
        def json(self):
            return {"id": "evt-789"}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append((url, headers, json))
        return DummyResponse()

    monkeypatch.setattr(integrations, "GOOGLE_CALENDAR_ACCESS_TOKEN", None)
    monkeypatch.setattr(integrations, "GOOGLE_CALENDAR_REFRESH_TOKEN", None)
    monkeypatch.setattr(integrations, "load_google_calendar_tokens", lambda path=None: ("saved-access", "saved-refresh"))
    monkeypatch.setattr(integrations, "request_google_calendar_authorization", lambda *args, **kwargs: "auth")
    monkeypatch.setattr(integrations.requests, "post", fake_post)

    response = integrations.create_google_calendar_event("Gym")

    assert response["id"] == "evt-789"
    assert calls[0][1]["Authorization"] == "Bearer saved-access"


def test_handle_calendar_command_reloads_saved_tokens_before_creating_event(monkeypatch):
    seen = {}

    def fake_load():
        integrations.GOOGLE_CALENDAR_ACCESS_TOKEN = "saved-access"
        integrations.GOOGLE_CALENDAR_REFRESH_TOKEN = "saved-refresh"

    def fake_create_event(title, details="", start_time=None, end_time=None, calendar_id="primary"):
        seen["access"] = integrations.GOOGLE_CALENDAR_ACCESS_TOKEN
        return {"id": "evt-999"}

    monkeypatch.setattr(integrations, "load_google_calendar_tokens_into_globals", fake_load)
    monkeypatch.setattr(integrations, "create_google_calendar_event", fake_create_event)

    reply = integrations.handle_calendar_command("schedule gym at 7pm today")

    assert "added to your calendar" in reply.lower()
    assert seen["access"] == "saved-access"


def test_parse_calendar_time_supports_standalone_time_with_tomorrow():
    start_time, end_time = integrations.parse_calendar_time("12:00 PM tomorrow")

    assert start_time is not None
    assert end_time is not None


def test_handle_calendar_command_pending_draft_accepts_standalone_time(monkeypatch):
    integrations.PENDING_CALENDAR_DRAFT = {"title": "Date with Tana", "command": "schedule date"}

    monkeypatch.setattr(integrations, "load_google_calendar_tokens_into_globals", lambda: ("access", "refresh"))
    monkeypatch.setattr(
        integrations,
        "create_google_calendar_event",
        lambda title, details="", start_time=None, end_time=None, calendar_id="primary": {
            "id": "evt-123",
            "summary": title,
            "start": {"dateTime": start_time},
            "end": {"dateTime": end_time},
        },
    )

    reply = integrations.handle_calendar_command("12:00 PM tomorrow")

    assert "added to your calendar" in reply.lower()
    assert integrations.PENDING_CALENDAR_DRAFT is None


def test_handle_google_calendar_callback_missing_code_raises():
    try:
        integrations.handle_google_calendar_callback({})
    except ValueError as exc:
        assert "Missing authorization code" in str(exc)
    else:
        raise AssertionError("Expected ValueError for missing code")


def test_request_google_calendar_authorization_starts_callback_server(monkeypatch):
    opened = []

    monkeypatch.setattr(integrations, "webbrowser", type("WB", (), {"open": staticmethod(lambda url: opened.append(url) or True)}))
    monkeypatch.setattr(integrations, "run_google_calendar_callback_server", lambda: None)
    monkeypatch.setattr(integrations, "get_google_auth_url", lambda scopes=None: "https://example.com/auth")

    response = integrations.request_google_calendar_authorization("Connect calendar")

    assert "Connect calendar" in response
    assert opened == ["https://example.com/auth"]


def test_handle_gmail_command_reads_latest_messages(monkeypatch):
    monkeypatch.setattr(integrations, "load_google_calendar_tokens_into_globals", lambda: ("access", "refresh"))
    monkeypatch.setattr(integrations, "GOOGLE_CALENDAR_ACCESS_TOKEN", "access")
    monkeypatch.setattr(integrations, "GOOGLE_CALENDAR_REFRESH_TOKEN", "refresh")
    monkeypatch.setattr(
        integrations,
        "list_gmail_messages",
        lambda max_results=5, query="in:inbox": [
            {"from": "Alice <alice@example.com>", "subject": "Hello", "id": "1", "date": "", "snippet": ""}
        ],
    )

    reply = integrations.handle_gmail_command("gmail inbox")

    assert "latest gmail messages" in reply.lower()
    assert "alice@example.com" in reply.lower()


def test_handle_gmail_command_sends_email(monkeypatch):
    sent = {}
    monkeypatch.setattr(integrations, "load_google_calendar_tokens_into_globals", lambda: ("access", "refresh"))
    monkeypatch.setattr(integrations, "GOOGLE_CALENDAR_ACCESS_TOKEN", "access")
    monkeypatch.setattr(integrations, "GOOGLE_CALENDAR_REFRESH_TOKEN", "refresh")

    def fake_send(to_email, subject, body):
        sent["to"] = to_email
        sent["subject"] = subject
        sent["body"] = body
        return {"id": "msg-1"}

    monkeypatch.setattr(integrations, "send_gmail_message", fake_send)

    reply = integrations.handle_gmail_command(
        "send email to bob@example.com subject Test message This is a test"
    )

    assert "gmail sent" in reply.lower()
    assert sent["to"] == "bob@example.com"
    assert sent["subject"] == "Test"
    assert sent["body"] == "This is a test"


def test_handle_gmail_command_sends_with_natural_email_phrase(monkeypatch):
    sent = {}
    monkeypatch.setattr(integrations, "load_google_calendar_tokens_into_globals", lambda: ("access", "refresh"))
    monkeypatch.setattr(integrations, "GOOGLE_CALENDAR_ACCESS_TOKEN", "access")
    monkeypatch.setattr(integrations, "GOOGLE_CALENDAR_REFRESH_TOKEN", "refresh")

    def fake_send(to_email, subject, body):
        sent["to"] = to_email
        sent["subject"] = subject
        sent["body"] = body
        return {"id": "msg-2"}

    monkeypatch.setattr(integrations, "send_gmail_message", fake_send)

    reply = integrations.handle_gmail_command(
        "email to bob at example dot com subject Hello message Checking in"
    )

    assert "gmail sent" in reply.lower()
    assert sent["to"] == "bob@example.com"
    assert sent["subject"] == "Hello"
    assert sent["body"] == "Checking in"


def test_handle_gmail_command_sends_with_about_and_saying_phrase(monkeypatch):
    sent = {}
    monkeypatch.setattr(integrations, "load_google_calendar_tokens_into_globals", lambda: ("access", "refresh"))
    monkeypatch.setattr(integrations, "GOOGLE_CALENDAR_ACCESS_TOKEN", "access")
    monkeypatch.setattr(integrations, "GOOGLE_CALENDAR_REFRESH_TOKEN", "refresh")

    def fake_send(to_email, subject, body):
        sent["to"] = to_email
        sent["subject"] = subject
        sent["body"] = body
        return {"id": "msg-3"}

    monkeypatch.setattr(integrations, "send_gmail_message", fake_send)

    reply = integrations.handle_gmail_command(
        "email to bob at example dot com about Meeting update saying I will be 10 minutes late"
    )

    assert "gmail sent" in reply.lower()
    assert sent["to"] == "bob@example.com"
    assert sent["subject"] == "Meeting update"
    assert sent["body"] == "I will be 10 minutes late"


def test_handle_gmail_command_contact_name_prompts_confirmation(monkeypatch):
    monkeypatch.setattr(integrations, "load_google_calendar_tokens_into_globals", lambda: ("access", "refresh"))
    monkeypatch.setattr(integrations, "GOOGLE_CALENDAR_ACCESS_TOKEN", "access")
    monkeypatch.setattr(integrations, "GOOGLE_CALENDAR_REFRESH_TOKEN", "refresh")
    monkeypatch.setattr(integrations, "_load_email_contacts", lambda path=None: {"bennet": "bennetk@gmail.com"})
    integrations.clear_pending_gmail_confirmation()

    reply = integrations.handle_gmail_command("email bennet subject Hello message Checking in")

    assert "bennetk@gmail.com" in reply.lower()
    assert "should i send it now" in reply.lower()
    assert integrations.has_pending_gmail_confirmation()


def test_handle_gmail_command_confirmation_yes_sends(monkeypatch):
    sent = {}
    monkeypatch.setattr(integrations, "load_google_calendar_tokens_into_globals", lambda: ("access", "refresh"))
    monkeypatch.setattr(integrations, "GOOGLE_CALENDAR_ACCESS_TOKEN", "access")
    monkeypatch.setattr(integrations, "GOOGLE_CALENDAR_REFRESH_TOKEN", "refresh")
    monkeypatch.setattr(integrations, "_load_email_contacts", lambda path=None: {"bennet": "bennetk@gmail.com"})

    def fake_send(to_email, subject, body):
        sent["to"] = to_email
        sent["subject"] = subject
        sent["body"] = body
        return {"id": "msg-3"}

    monkeypatch.setattr(integrations, "send_gmail_message", fake_send)
    integrations.clear_pending_gmail_confirmation()

    prompt_reply = integrations.handle_gmail_command("email bennet subject Hello message Checking in")
    yes_reply = integrations.handle_gmail_command("yes")

    assert "bennetk@gmail.com" in prompt_reply.lower()
    assert "gmail sent" in yes_reply.lower()
    assert sent["to"] == "bennetk@gmail.com"
    assert sent["subject"] == "Hello"
    assert sent["body"] == "Checking in"
    assert not integrations.has_pending_gmail_confirmation()


def test_route_command_uses_pending_gmail_confirmation_for_yes(monkeypatch):
    monkeypatch.setattr(main, "speak", lambda _text: None)
    monkeypatch.setattr(main, "remember", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "handle_gmail_command", lambda command: f"gmail handled: {command}")
    monkeypatch.setattr(main, "has_pending_gmail_confirmation", lambda: True)

    reply = main.route_command("yes", personality="", memory=[])

    assert reply == "gmail handled: yes"


def test_route_command_opens_slicer_on_open_intent(monkeypatch):
    monkeypatch.setattr(main, "speak", lambda _text: None)
    monkeypatch.setattr(main, "remember", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "open_slicer_app", lambda: (True, "Opening slicer at C:/Program Files/AnycubicSlicer/AnycubicSlicer.exe."))

    reply = main.route_command("can you open any cubic slicer", personality="", memory=[])

    assert "opening slicer" in reply.lower()
