import datetime
import json
import os
import re
import ssl
import threading
import urllib.parse
import webbrowser
import base64
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

SPOTIFY_ACCESS_TOKEN = None
SPOTIFY_REFRESH_TOKEN = None
GOOGLE_CALENDAR_REDIRECT_URI = os.getenv("GOOGLE_CALENDAR_REDIRECT_URI", "http://localhost:8000/callback")


def get_env_file_path(path=None):
    return path or os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")


def load_environment_file(path=None):
    env_path = get_env_file_path(path)
    if not os.path.exists(env_path):
        return False

    with open(env_path, encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

    return True


def load_google_calendar_tokens(path=None):
    token_path = path or GOOGLE_CALENDAR_TOKEN_FILE
    if os.path.exists(token_path):
        try:
            with open(token_path, encoding="utf-8") as handle:
                data = json.load(handle)
            return data.get("access_token"), data.get("refresh_token")
        except Exception:
            return None, None
    return None, None


def save_google_calendar_tokens(access_token, refresh_token=None, path=None):
    token_path = path or GOOGLE_CALENDAR_TOKEN_FILE
    data = {
        "access_token": access_token,
        "refresh_token": refresh_token,
    }
    with open(token_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    if access_token is not None:
        os.environ["GOOGLE_CALENDAR_ACCESS_TOKEN"] = access_token
    if refresh_token is not None:
        os.environ["GOOGLE_CALENDAR_REFRESH_TOKEN"] = refresh_token


def load_google_calendar_tokens_into_globals(path=None):
    global GOOGLE_CALENDAR_ACCESS_TOKEN, GOOGLE_CALENDAR_REFRESH_TOKEN
    access_token, refresh_token = load_google_calendar_tokens(path)
    if access_token is not None:
        GOOGLE_CALENDAR_ACCESS_TOKEN = access_token
        os.environ["GOOGLE_CALENDAR_ACCESS_TOKEN"] = access_token
    if refresh_token is not None:
        GOOGLE_CALENDAR_REFRESH_TOKEN = refresh_token
        os.environ["GOOGLE_CALENDAR_REFRESH_TOKEN"] = refresh_token
    return GOOGLE_CALENDAR_ACCESS_TOKEN, GOOGLE_CALENDAR_REFRESH_TOKEN


load_environment_file()
GOOGLE_CALENDAR_CLIENT_ID = os.getenv("GOOGLE_CALENDAR_CLIENT_ID")
GOOGLE_CALENDAR_CLIENT_SECRET = os.getenv("GOOGLE_CALENDAR_CLIENT_SECRET")
GOOGLE_CALENDAR_ACCESS_TOKEN = os.getenv("GOOGLE_CALENDAR_ACCESS_TOKEN")
GOOGLE_CALENDAR_REFRESH_TOKEN = os.getenv("GOOGLE_CALENDAR_REFRESH_TOKEN")
GOOGLE_CALENDAR_TOKEN_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "google_calendar_tokens.json")
file_access_token, file_refresh_token = load_google_calendar_tokens()
if not GOOGLE_CALENDAR_ACCESS_TOKEN and file_access_token:
    GOOGLE_CALENDAR_ACCESS_TOKEN = file_access_token
if not GOOGLE_CALENDAR_REFRESH_TOKEN and file_refresh_token:
    GOOGLE_CALENDAR_REFRESH_TOKEN = file_refresh_token


SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "e6c88f3f2d4448cebc8e55ee55566ef0")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "7c3833bdd262444aa17034dea98133f0")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "http://localhost:8888/callback")
SPOTIFY_SCOPE = "user-read-playback-state user-modify-playback-state user-library-read playlist-read-private"


def _get_or_create_self_signed_cert(cert_dir=None):
    """Generate a self-signed certificate for localhost HTTPS. Returns (cert_path, key_path)."""
    if cert_dir is None:
        cert_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".ssl")
    
    os.makedirs(cert_dir, exist_ok=True)
    cert_path = os.path.join(cert_dir, "localhost.crt")
    key_path = os.path.join(cert_dir, "localhost.key")
    
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return cert_path, key_path
    
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        import datetime as dt
        
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, u"US"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, u"Future"),
            x509.NameAttribute(NameOID.COMMON_NAME, u"localhost"),
        ])
        cert = x509.CertificateBuilder().subject_name(
            subject
        ).issuer_name(
            issuer
        ).public_key(
            private_key.public_key()
        ).serial_number(
            x509.random_serial_number()
        ).not_valid_before(
            dt.datetime.utcnow()
        ).not_valid_after(
            dt.datetime.utcnow() + dt.timedelta(days=365)
        ).add_extension(
            x509.SubjectAlternativeName([x509.DNSName(u"localhost"), x509.DNSName(u"127.0.0.1")]),
            critical=False,
        ).sign(private_key, hashes.SHA256())
        
        with open(cert_path, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        
        with open(key_path, "wb") as f:
            f.write(private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption()
            ))
        
        return cert_path, key_path
    except ImportError:
        pass
    
    return None, None


def build_spotify_url(query):
    encoded = urllib.parse.quote(query)
    return f"https://open.spotify.com/search/{encoded}"


def build_spotify_deeplink(query):
    encoded = urllib.parse.quote(query)
    return f"spotify:search:{encoded}"


def get_spotify_auth_url():
    params = {
        "client_id": SPOTIFY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": SPOTIFY_REDIRECT_URI,
        "scope": SPOTIFY_SCOPE,
        "show_dialog": "true",
    }
    return "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(params)


def exchange_spotify_code(code):
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        raise RuntimeError("Spotify API credentials are not configured")

    response = requests.post(
        "https://accounts.spotify.com/api/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": SPOTIFY_REDIRECT_URI,
        },
        auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def refresh_spotify_token(refresh_token):
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        raise RuntimeError("Spotify API credentials are not configured")

    response = requests.post(
        "https://accounts.spotify.com/api/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def validate_spotify_credentials():
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        raise RuntimeError("Spotify API credentials are not configured")

    response = requests.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def set_spotify_tokens(access_token, refresh_token=None):
    global SPOTIFY_ACCESS_TOKEN, SPOTIFY_REFRESH_TOKEN
    SPOTIFY_ACCESS_TOKEN = access_token
    SPOTIFY_REFRESH_TOKEN = refresh_token
    
    spotify_token_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "spotify_tokens.json")
    data = {"access_token": access_token, "refresh_token": refresh_token}
    with open(spotify_token_file, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)


def load_spotify_tokens(path=None):
    token_path = path or os.path.join(os.path.dirname(os.path.dirname(__file__)), "spotify_tokens.json")
    if os.path.exists(token_path):
        try:
            with open(token_path, encoding="utf-8") as handle:
                data = json.load(handle)
            return data.get("access_token"), data.get("refresh_token")
        except Exception:
            return None, None
    return None, None


def load_spotify_tokens_into_globals():
    global SPOTIFY_ACCESS_TOKEN, SPOTIFY_REFRESH_TOKEN
    access_token, refresh_token = load_spotify_tokens()
    if access_token is not None:
        SPOTIFY_ACCESS_TOKEN = access_token
    if refresh_token is not None:
        SPOTIFY_REFRESH_TOKEN = refresh_token
    return SPOTIFY_ACCESS_TOKEN, SPOTIFY_REFRESH_TOKEN


def exchange_spotify_code_manual(code):
    """Manually exchange an auth code for tokens (for local development without a proper callback)."""
    token_data = exchange_spotify_code(code)
    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    if not access_token:
        raise RuntimeError("Spotify did not return an access token.")
    
    set_spotify_tokens(access_token, refresh_token)
    return "Spotify connected successfully."


def set_google_calendar_tokens(access_token, refresh_token=None):
    global GOOGLE_CALENDAR_ACCESS_TOKEN, GOOGLE_CALENDAR_REFRESH_TOKEN
    GOOGLE_CALENDAR_ACCESS_TOKEN = access_token
    GOOGLE_CALENDAR_REFRESH_TOKEN = refresh_token

    save_google_calendar_tokens(access_token, refresh_token)


def _parse_time_component(raw_hour, raw_minute, raw_ampm, date):
    hour = int(raw_hour)
    minute = int(raw_minute or 0)
    ampm = raw_ampm
    if ampm:
        ampm = ampm.lower()
        if ampm == "pm" and hour != 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
    return datetime.datetime(date.year, date.month, date.day, hour, minute)


def _format_datetime(dt):
    tz = datetime.datetime.now().astimezone().tzinfo
    return dt.replace(tzinfo=tz).isoformat()


def parse_calendar_time(command):
    command_lower = command.lower()
    command_lower = command_lower.replace("o clock", "")
    command_lower = command_lower.replace("tmrw", "tomorrow")
    command_lower = command_lower.replace("tmr", "tomorrow")
    command_lower = command_lower.replace("2morrow", "tomorrow")
    command_lower = re.sub(r"\bnoon\b", "12 pm", command_lower)
    command_lower = re.sub(r"\bmidnight\b", "12 am", command_lower)
    word_to_hour = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "eleven": 11,
        "twelve": 12,
    }
    for word, num in word_to_hour.items():
        command_lower = re.sub(rf"\b{word}\b", str(num), command_lower)

    now = datetime.datetime.now()
    date = now.date()

    if "tomorrow" in command_lower:
        date += datetime.timedelta(days=1)
    elif "today" in command_lower or "tonight" in command_lower:
        date = now.date()

    weekdays = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }
    for weekday_name, weekday_index in weekdays.items():
        if weekday_name in command_lower:
            days_ahead = (weekday_index - now.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            date = now.date() + datetime.timedelta(days=days_ahead)
            break

    start_match = re.search(r"\b(?:at|for)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", command_lower)
    if not start_match:
        # Support follow-up replies like "12pm tomorrow" without requiring "at"/"for".
        start_match = re.search(r"\b(\d{1,2})(?::(\d{2}))\s*(am|pm)\b", command_lower)
    if not start_match:
        start_match = re.search(r"\b(\d{1,2})\s*(am|pm)\b", command_lower)
    start_time = None
    if start_match:
        ampm = start_match.group(3)
        if not ampm and int(start_match.group(1)) <= 7:
            ampm = "pm"
        start_time = _parse_time_component(start_match.group(1), start_match.group(2), ampm, date)

    end_match = re.search(r"\b(?:until|to)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", command_lower)
    end_time = None
    if end_match:
        end_time = _parse_time_component(end_match.group(1), end_match.group(2), end_match.group(3), date)

    if start_time and not end_time:
        end_time = start_time + datetime.timedelta(hours=1)

    if start_time:
        start_time = _format_datetime(start_time)
    if end_time:
        end_time = _format_datetime(end_time)

    return start_time, end_time


GOOGLE_CALENDAR_CALLBACK_SERVER = None
GOOGLE_CALENDAR_CALLBACK_SERVER_THREAD = None

GOOGLE_CALENDAR_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
]
GOOGLE_GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]
GOOGLE_COMBINED_SCOPES = GOOGLE_CALENDAR_SCOPES + GOOGLE_GMAIL_SCOPES
EMAIL_CONTACTS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "email_contacts.json")
PENDING_GMAIL_CONFIRMATION = None
PENDING_CALENDAR_DRAFT = None


def _get_callback_value(query, key):
    value = query.get(key)
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


class _GoogleCalendarOAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        expected_path = urllib.parse.urlparse(GOOGLE_CALENDAR_REDIRECT_URI).path
        if parsed.path != expected_path:
            self.send_error(404, "Not found")
            return

        query = urllib.parse.parse_qs(parsed.query)
        status = 200
        try:
            if "code" in query:
                message = handle_google_calendar_callback({"code": query["code"][0]})
                body = f"<html><body><h1>Google Calendar Connected</h1><p>{message}</p></body></html>"
            elif "error" in query:
                error_description = query.get("error_description", [query.get("error", ["Authorization failed"])[0]])[0]
                body = f"<html><body><h1>Authorization failed</h1><p>{urllib.parse.unquote(error_description)}</p></body></html>"
                status = 400
            else:
                body = "<html><body><h1>Bad Request</h1><p>Missing authorization code.</p></body></html>"
                status = 400
        except Exception as exc:
            status = 500
            body = f"<html><body><h1>Server error</h1><p>{exc}</p></body></html>"

        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

        if status == 200:
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    def log_message(self, format, *args):
        return


def run_google_calendar_callback_server(timeout=120):
    global GOOGLE_CALENDAR_CALLBACK_SERVER, GOOGLE_CALENDAR_CALLBACK_SERVER_THREAD
    if GOOGLE_CALENDAR_CALLBACK_SERVER is not None:
        return GOOGLE_CALENDAR_CALLBACK_SERVER

    parsed_uri = urllib.parse.urlparse(GOOGLE_CALENDAR_REDIRECT_URI)
    host = parsed_uri.hostname or "localhost"
    bind_host = "" if host in ("localhost", "127.0.0.1") else host
    port = parsed_uri.port or 8000

    try:
        server = HTTPServer((bind_host, port), _GoogleCalendarOAuthCallbackHandler)
    except OSError as exc:
        raise RuntimeError(f"Unable to start local callback server on {host}:{port}: {exc}") from exc

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    GOOGLE_CALENDAR_CALLBACK_SERVER = server
    GOOGLE_CALENDAR_CALLBACK_SERVER_THREAD = thread
    return server


def stop_google_calendar_callback_server():
    global GOOGLE_CALENDAR_CALLBACK_SERVER, GOOGLE_CALENDAR_CALLBACK_SERVER_THREAD
    if GOOGLE_CALENDAR_CALLBACK_SERVER is None:
        return

    try:
        GOOGLE_CALENDAR_CALLBACK_SERVER.shutdown()
    finally:
        GOOGLE_CALENDAR_CALLBACK_SERVER.server_close()
        GOOGLE_CALENDAR_CALLBACK_SERVER = None
        GOOGLE_CALENDAR_CALLBACK_SERVER_THREAD = None


SPOTIFY_CALLBACK_SERVER = None
SPOTIFY_CALLBACK_SERVER_THREAD = None


class _SpotifyOAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        expected_path = urllib.parse.urlparse(SPOTIFY_REDIRECT_URI).path
        if parsed.path != expected_path:
            self.send_error(404, "Not found")
            return

        query = urllib.parse.parse_qs(parsed.query)
        status = 200
        try:
            if "code" in query:
                code = query["code"][0]
                token_data = exchange_spotify_code(code)
                access_token = token_data.get("access_token")
                refresh_token = token_data.get("refresh_token")
                if access_token:
                    set_spotify_tokens(access_token, refresh_token)
                    message = "Spotify Connected"
                    body = "<html><body><h1>Spotify Connected</h1><p>You can close this window.</p></body></html>"
                else:
                    message = "Spotify connection failed"
                    body = "<html><body><h1>Connection Failed</h1><p>No access token received.</p></body></html>"
                    status = 500
            elif "error" in query:
                error_description = query.get("error_description", [query.get("error", ["Authorization failed"])[0]])[0]
                body = f"<html><body><h1>Authorization failed</h1><p>{urllib.parse.unquote(error_description)}</p></body></html>"
                status = 400
            else:
                body = "<html><body><h1>Bad Request</h1><p>Missing authorization code.</p></body></html>"
                status = 400
        except Exception as exc:
            status = 500
            body = f"<html><body><h1>Server error</h1><p>{exc}</p></body></html>"

        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

        if status == 200:
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    def log_message(self, format, *args):
        return


def run_spotify_callback_server(timeout=120):
    global SPOTIFY_CALLBACK_SERVER, SPOTIFY_CALLBACK_SERVER_THREAD
    if SPOTIFY_CALLBACK_SERVER is not None:
        return SPOTIFY_CALLBACK_SERVER

    parsed_uri = urllib.parse.urlparse(SPOTIFY_REDIRECT_URI)
    port = parsed_uri.port or 8888

    try:
        server = HTTPServer(("", port), _SpotifyOAuthCallbackHandler)
    except OSError as exc:
        raise RuntimeError(f"Unable to start Spotify callback server on localhost:{port}: {exc}") from exc

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    SPOTIFY_CALLBACK_SERVER = server
    SPOTIFY_CALLBACK_SERVER_THREAD = thread
    return server


def stop_spotify_callback_server():
    global SPOTIFY_CALLBACK_SERVER, SPOTIFY_CALLBACK_SERVER_THREAD
    if SPOTIFY_CALLBACK_SERVER is None:
        return

    try:
        SPOTIFY_CALLBACK_SERVER.shutdown()
    finally:
        SPOTIFY_CALLBACK_SERVER.server_close()
        SPOTIFY_CALLBACK_SERVER = None
        SPOTIFY_CALLBACK_SERVER_THREAD = None


def complete_google_calendar_authorization(code):
    token_data = exchange_google_calendar_code(code)
    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    if not access_token:
        raise RuntimeError("Google did not return an access token.")

    set_google_calendar_tokens(access_token, refresh_token)
    return "Google Calendar connected successfully."


def handle_google_calendar_callback(query):
    code = _get_callback_value(query, "code")
    error = _get_callback_value(query, "error")
    if error:
        raise RuntimeError(f"Google Calendar authorization failed: {error}")
    if not code:
        raise ValueError("Missing authorization code in callback request.")

    return complete_google_calendar_authorization(code)


def get_spotify_headers():
    return get_spotify_headers_for_mode(require_user=False)


def _ensure_spotify_user_token(force_refresh=False):
    load_spotify_tokens_into_globals()
    if SPOTIFY_ACCESS_TOKEN and not force_refresh:
        return True
    if not SPOTIFY_REFRESH_TOKEN:
        return bool(SPOTIFY_ACCESS_TOKEN)

    token_data = refresh_spotify_token(SPOTIFY_REFRESH_TOKEN)
    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token", SPOTIFY_REFRESH_TOKEN)
    if not access_token:
        return False
    set_spotify_tokens(access_token, refresh_token)
    return True


def get_spotify_headers_for_mode(require_user=False):
    if require_user:
        if _ensure_spotify_user_token():
            return {"Authorization": f"Bearer {SPOTIFY_ACCESS_TOKEN}"}
        raise RuntimeError("Spotify user access token not available")

    if SPOTIFY_ACCESS_TOKEN:
        return {"Authorization": f"Bearer {SPOTIFY_ACCESS_TOKEN}"}

    try:
        token_data = validate_spotify_credentials()
    except Exception as exc:
        raise RuntimeError("Spotify access token not available") from exc

    return {"Authorization": f"Bearer {token_data['access_token']}"}


def _spotify_user_request(method, url, params=None, payload=None, timeout=20):
    headers = get_spotify_headers_for_mode(require_user=True)
    response = requests.request(method, url, headers=headers, params=params, json=payload, timeout=timeout)
    if response.status_code == 401 and SPOTIFY_REFRESH_TOKEN:
        if _ensure_spotify_user_token(force_refresh=True):
            headers = get_spotify_headers_for_mode(require_user=True)
            response = requests.request(method, url, headers=headers, params=params, json=payload, timeout=timeout)
    response.raise_for_status()
    return response


def spotify_search(query, limit=5):
    headers = get_spotify_headers_for_mode(require_user=False)
    params = {"q": query, "type": "track", "limit": limit}
    response = requests.get("https://api.spotify.com/v1/search", headers=headers, params=params, timeout=20)
    response.raise_for_status()
    return response.json()


def spotify_play(track_uri=None):
    payload = {}
    if track_uri:
        payload = {"uris": [track_uri]}
    response = _spotify_user_request("PUT", "https://api.spotify.com/v1/me/player/play", payload=payload, timeout=20)
    return response.status_code == 204


def spotify_add_to_queue(track_uri):
    response = _spotify_user_request(
        "POST",
        "https://api.spotify.com/v1/me/player/queue",
        params={"uri": track_uri},
        timeout=20,
    )
    return response.status_code == 204


def spotify_pause():
    response = _spotify_user_request("PUT", "https://api.spotify.com/v1/me/player/pause", timeout=20)
    return response.status_code == 204


def spotify_next():
    response = _spotify_user_request("POST", "https://api.spotify.com/v1/me/player/next", timeout=20)
    return response.status_code == 204


def spotify_previous():
    response = _spotify_user_request("POST", "https://api.spotify.com/v1/me/player/previous", timeout=20)
    return response.status_code == 204


def spotify_set_volume(percent):
    percent = max(0, min(100, int(percent)))
    response = _spotify_user_request(
        "PUT",
        "https://api.spotify.com/v1/me/player/volume",
        params={"volume_percent": percent},
        timeout=20,
    )
    return response.status_code == 204


def get_spotify_now_playing():
    """Return normalized current playback details for dashboard usage."""
    response = _spotify_user_request("GET", "https://api.spotify.com/v1/me/player", timeout=20)
    if response.status_code == 204 or not response.text:
        return {
            "is_playing": False,
            "track": "Nothing playing",
            "artist": "",
            "album_image_url": "",
            "elapsed_sec": 0,
            "duration_sec": 0,
        }

    payload = response.json() or {}
    item = payload.get("item") or {}
    album = item.get("album") or {}
    images = album.get("images") or []
    image_url = ""
    if isinstance(images, list):
        for image in images:
            if isinstance(image, dict) and image.get("url"):
                image_url = str(image.get("url"))
                break
    artists = item.get("artists") or []
    artist_name = ", ".join(
        artist.get("name", "") for artist in artists if isinstance(artist, dict) and artist.get("name")
    )
    return {
        "is_playing": bool(payload.get("is_playing")),
        "track": item.get("name") or "Nothing playing",
        "artist": artist_name,
        "album_image_url": image_url,
        "elapsed_sec": int((payload.get("progress_ms") or 0) / 1000),
        "duration_sec": int((item.get("duration_ms") or 0) / 1000),
    }


def _prepare_spotify_query(command):
    lowered = command.lower()

    # If the request contains other intents, keep only the music clause.
    cue_matches = list(re.finditer(r"\b(play|queue|put on|listen to)\b", lowered))
    if cue_matches:
        lowered = lowered[cue_matches[-1].start():]

    cleaned = lowered
    cleaned = re.sub(r"\b(on|in|from)\s+spotify\b", " ", cleaned)
    cleaned = re.sub(r"\bspotify\b", " ", cleaned)
    cleaned = re.sub(r"\bplay\s+music\b", " ", cleaned)
    cleaned = re.sub(r"^\s*(play|queue|put on|listen to)\s+", "", cleaned)
    cleaned = re.sub(r"^\s*(some|a|an|the)\s+", "", cleaned)
    cleaned = re.sub(r"\b(please|for me|right now)\b", " ", cleaned)
    cleaned = re.sub(r"\s+\bon\b$", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,!?")
    return cleaned or "chill music"


def _choose_best_spotify_track(items, query):
    def _tokens(value):
        return set(re.findall(r"[a-z0-9']+", (value or "").lower()))

    query_tokens = _tokens(query)
    best_item = None
    best_score = float("-inf")

    for idx, item in enumerate(items):
        track_name = str(item.get("name", ""))
        artist_name = " ".join(
            str(artist.get("name", "")) for artist in (item.get("artists") or []) if isinstance(artist, dict)
        )
        haystack = f"{track_name} {artist_name}".lower()
        hay_tokens = _tokens(haystack)

        overlap = len(query_tokens & hay_tokens)
        score = overlap * 8
        if query and query.lower() in haystack:
            score += 20
        if query and query.lower() == track_name.lower():
            score += 30
        if query and query.lower() == artist_name.lower():
            score += 25

        # Prefer earlier API results when scores tie.
        score -= idx * 0.01

        if score > best_score:
            best_score = score
            best_item = item

    return best_item or (items[0] if items else None)


def request_spotify_authorization(reason="Spotify playback control needs authorization."):
    auth_url = get_spotify_auth_url()
    callback_message = ""
    try:
        run_spotify_callback_server()
        callback_message = "The local callback listener is running and will complete the connection automatically."
    except Exception as exc:
        callback_message = (
            "I could not start the local callback listener. If the browser does not redirect automatically, "
            "copy the authorization code from the URL and call exchange_spotify_code_manual(code)."
        )

    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    return f"{reason} I opened the Spotify authorization page so you can connect your account. {callback_message}"


def _spotify_error_response(exc):
    response = getattr(exc, "response", None)
    if response is None:
        return None, ""

    status_code = getattr(response, "status_code", None)
    detail = ""
    try:
        payload = response.json() or {}
        if isinstance(payload, dict):
            error_obj = payload.get("error")
            if isinstance(error_obj, dict):
                detail = str(error_obj.get("reason") or error_obj.get("message") or "").strip()
            elif isinstance(error_obj, str):
                detail = error_obj.strip()
    except Exception:
        pass

    if not detail:
        try:
            detail = (response.text or "").strip()
        except Exception:
            detail = ""
    return status_code, detail


def _run_spotify_control_action(action, auth_reason, success_message):
    if not _ensure_spotify_user_token():
        return request_spotify_authorization(auth_reason)

    try:
        action()
        return success_message
    except requests.HTTPError as exc:
        status_code, detail = _spotify_error_response(exc)
        detail_upper = (detail or "").upper()

        if status_code in {401, 403}:
            return request_spotify_authorization(auth_reason)
        if status_code == 404 and "NO_ACTIVE_DEVICE" in detail_upper:
            return "Spotify is connected, but no active playback device was found. Open Spotify on your phone or desktop and try again."
        if status_code == 429:
            return "Spotify rate-limited the request. Wait a few seconds and try again."
        if detail:
            return f"Spotify command failed: {detail}"
        return "Spotify command failed."
    except Exception as exc:
        return f"Spotify command failed: {exc}"


def _is_resume_spotify_intent(command_lower):
    if any(token in command_lower for token in ["unpause", "resume", "continue playback", "continue spotify"]):
        return True

    return bool(re.fullmatch(r"\s*play(?:\s+spotify)?\s*", command_lower))


def handle_spotify_command(command):
    load_spotify_tokens_into_globals()
    command_lower = command.lower()

    if _is_resume_spotify_intent(command_lower):
        return _run_spotify_control_action(
            spotify_play,
            "Playback resume needs your Spotify account connected.",
            "Resuming Spotify.",
        )

    if "pause" in command_lower:
        return _run_spotify_control_action(
            spotify_pause,
            "Playback pause needs your Spotify account connected.",
            "Pausing Spotify.",
        )

    if "next" in command_lower or "skip" in command_lower:
        return _run_spotify_control_action(
            spotify_next,
            "Skipping tracks needs your Spotify account connected.",
            "Skipping to the next track.",
        )

    if "previous" in command_lower or "go back" in command_lower:
        return _run_spotify_control_action(
            spotify_previous,
            "Going back needs your Spotify account connected.",
            "Going back to the previous track.",
        )

    if "volume" in command_lower:
        try:
            if "up" in command_lower:
                percent = 80
            elif "down" in command_lower:
                percent = 40
            else:
                try:
                    percent = int(command_lower.split("volume", 1)[1].strip().replace("%", ""))
                except Exception:
                    percent = 50

            return _run_spotify_control_action(
                lambda: spotify_set_volume(percent),
                "Volume control needs your Spotify account connected.",
                f"Setting Spotify volume to {percent} percent.",
            )
        except Exception as exc:
            return f"Spotify command failed: {exc}"

    query = _prepare_spotify_query(command)

    try:
        results = spotify_search(query)
    except Exception:
        open_spotify(query)
        return f"Opening Spotify for {query}."

    items = results.get("tracks", {}).get("items", [])
    if items:
        track = _choose_best_spotify_track(items, query)
        uri = track.get("uri")
        external_url = track.get("external_urls", {}).get("spotify")
        artists = track.get("artists", [])
        artist_name = artists[0].get("name", "") if artists else ""
        track_name = track.get("name", query)
        if not _ensure_spotify_user_token():
            return request_spotify_authorization("Playing and queueing tracks needs your Spotify account connected.")
        try:
            if uri:
                spotify_add_to_queue(uri)
                spotify_play()
            else:
                spotify_play(uri)
        except Exception:
            try:
                spotify_play(uri)
            except Exception:
                opened = open_spotify_track(uri, external_url)
                if opened:
                    if artist_name:
                        return f"Opening {track_name} by {artist_name} on Spotify."
                    return f"Opening {track_name} on Spotify."
                open_spotify(query)
                return f"Opening Spotify for {query}."
        if artist_name:
            return f"Queued and playing {track_name} by {artist_name}."
        return f"Queued and playing {track_name}."

    open_spotify(query)
    return f"Opening Spotify for {query}."


def open_spotify(query):
    url = build_spotify_url(query)
    deeplink = build_spotify_deeplink(query)
    try:
        webbrowser.open(deeplink)
    except Exception:
        webbrowser.open(url)
    return url


def open_spotify_track(track_uri=None, track_url=None):
    if track_uri:
        try:
            webbrowser.open(track_uri)
            return track_uri
        except Exception:
            pass

    if track_url:
        webbrowser.open(track_url)
        return track_url

    return None


def build_google_calendar_event_url(title, details="", start_time=None):
    params = {
        "action": "TEMPLATE",
        "text": title,
        "details": details,
    }
    if start_time:
        params["dates"] = start_time
    return "https://calendar.google.com/calendar/render?" + urllib.parse.urlencode(params)


def open_google_calendar_event(title, details="", start_time=None):
    url = build_google_calendar_event_url(title, details, start_time)
    webbrowser.open(url)
    return url


def get_google_calendar_auth_url():
    return get_google_auth_url(scopes=GOOGLE_COMBINED_SCOPES)


def get_google_auth_url(scopes=None):
    scopes = scopes or GOOGLE_COMBINED_SCOPES
    params = {
        "client_id": GOOGLE_CALENDAR_CLIENT_ID or "",
        "redirect_uri": GOOGLE_CALENDAR_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)


def exchange_google_calendar_code(code):
    if not GOOGLE_CALENDAR_CLIENT_ID or not GOOGLE_CALENDAR_CLIENT_SECRET:
        raise RuntimeError("Google Calendar credentials are not configured")

    response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": GOOGLE_CALENDAR_CLIENT_ID,
            "client_secret": GOOGLE_CALENDAR_CLIENT_SECRET,
            "redirect_uri": GOOGLE_CALENDAR_REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def refresh_google_calendar_token(refresh_token):
    if not GOOGLE_CALENDAR_CLIENT_ID or not GOOGLE_CALENDAR_CLIENT_SECRET:
        raise RuntimeError("Google Calendar credentials are not configured")

    response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": GOOGLE_CALENDAR_CLIENT_ID,
            "client_secret": GOOGLE_CALENDAR_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def _refresh_google_calendar_access_token():
    if not GOOGLE_CALENDAR_REFRESH_TOKEN:
        raise RuntimeError("Google Calendar refresh token not available")

    token_data = refresh_google_calendar_token(GOOGLE_CALENDAR_REFRESH_TOKEN)
    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token", GOOGLE_CALENDAR_REFRESH_TOKEN)
    if not access_token:
        raise RuntimeError("Google did not return an access token while refreshing.")
    set_google_calendar_tokens(access_token, refresh_token)


def create_google_calendar_event(title, details="", start_time=None, end_time=None, calendar_id="primary"):
    load_google_calendar_tokens_into_globals()
    if not GOOGLE_CALENDAR_ACCESS_TOKEN and not GOOGLE_CALENDAR_REFRESH_TOKEN:
        raise RuntimeError("Google Calendar access token not available")

    payload = {
        "summary": title,
        "description": details,
    }
    if start_time:
        payload["start"] = {"dateTime": start_time}
        if end_time:
            payload["end"] = {"dateTime": end_time}
        else:
            parsed_start = datetime.datetime.fromisoformat(start_time)
            if parsed_start.tzinfo is None:
                parsed_start = parsed_start.replace(tzinfo=datetime.datetime.now().astimezone().tzinfo)
            default_end = parsed_start + datetime.timedelta(hours=1)
            payload["end"] = {"dateTime": default_end.isoformat()}

    headers = {"Authorization": f"Bearer {GOOGLE_CALENDAR_ACCESS_TOKEN}"}
    response = requests.post(
        f"https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(calendar_id)}/events",
        headers=headers,
        json=payload,
        timeout=20,
    )
    if response.status_code == 401 and GOOGLE_CALENDAR_REFRESH_TOKEN:
        _refresh_google_calendar_access_token()
        headers = {"Authorization": f"Bearer {GOOGLE_CALENDAR_ACCESS_TOKEN}"}
        response = requests.post(
            f"https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(calendar_id)}/events",
            headers=headers,
            json=payload,
            timeout=20,
        )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not str(payload.get("id", "")).strip():
        raise RuntimeError("Google Calendar did not confirm event creation.")
    return payload


def list_google_calendar_events(range_name="today", max_results=8, calendar_id="primary"):
    """List upcoming events and normalize them for UI rendering."""
    load_google_calendar_tokens_into_globals()
    if not GOOGLE_CALENDAR_ACCESS_TOKEN and not GOOGLE_CALENDAR_REFRESH_TOKEN:
        raise RuntimeError("Google Calendar access token not available")

    now = datetime.datetime.now().astimezone()
    range_name = (range_name or "today").strip().lower()
    if range_name == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + datetime.timedelta(days=1)
    elif range_name == "tomorrow":
        start = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + datetime.timedelta(days=1)
    elif range_name in {"week", "next7", "7d"}:
        start = now
        end = now + datetime.timedelta(days=7)
    elif range_name in {"month", "this month", "current month"}:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
    else:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + datetime.timedelta(days=1)

    params = {
        "singleEvents": "true",
        "orderBy": "startTime",
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "maxResults": max_results,
    }

    headers = {"Authorization": f"Bearer {GOOGLE_CALENDAR_ACCESS_TOKEN}"}
    response = requests.get(
        f"https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(calendar_id)}/events",
        headers=headers,
        params=params,
        timeout=20,
    )
    if response.status_code == 401 and GOOGLE_CALENDAR_REFRESH_TOKEN:
        _refresh_google_calendar_access_token()
        headers = {"Authorization": f"Bearer {GOOGLE_CALENDAR_ACCESS_TOKEN}"}
        response = requests.get(
            f"https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(calendar_id)}/events",
            headers=headers,
            params=params,
            timeout=20,
        )
    response.raise_for_status()

    events = []
    for item in response.json().get("items", []):
        start_info = item.get("start", {})
        start_raw = start_info.get("dateTime") or start_info.get("date")
        display_time = "All day"
        display_date = ""
        if start_raw:
            try:
                if "T" in str(start_raw):
                    start_dt = datetime.datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                    local_dt = start_dt.astimezone()
                    display_time = local_dt.strftime("%I:%M %p").lstrip("0")
                    display_date = local_dt.date().isoformat()
                else:
                    display_date = str(start_raw)
            except Exception:
                display_time = str(start_raw)
                display_date = str(start_raw)[:10]

        events.append(
            {
                "time": display_time,
                "date": display_date,
                "title": item.get("summary") or "(untitled event)",
                "subtitle": item.get("location") or item.get("description") or "",
            }
        )

    return events


def request_google_calendar_authorization(reason="Google Calendar needs authorization."):
    return request_google_authorization(reason=reason, scopes=GOOGLE_COMBINED_SCOPES)


def request_google_authorization(reason="Google services need authorization.", scopes=None):
    auth_url = get_google_auth_url(scopes=scopes or GOOGLE_COMBINED_SCOPES)
    callback_message = ""
    try:
        run_google_calendar_callback_server()
        callback_message = "The local callback listener is running and will complete the connection automatically."
    except Exception as exc:
        callback_message = (
            "I could not start the local callback listener. If the browser does not redirect automatically, "
            "copy the authorization code from the URL and call complete_google_calendar_authorization(code)."
        )

    try:
        webbrowser.open(auth_url)
    except Exception:
        callback_message = (
            "I could not open the browser automatically. Paste this URL into a browser to continue: "
            f"{auth_url}"
        )

    return f"{reason} I opened the Google authorization page so you can connect your account. {callback_message}"


def request_google_gmail_authorization(reason="Gmail needs authorization."):
    return request_google_authorization(reason=reason, scopes=GOOGLE_COMBINED_SCOPES)


def _normalize_calendar_title(command):
    text = " ".join((command or "").strip().split())
    text = re.sub(r"[?.!]+$", "", text)
    text = re.sub(r"^(?:hey\s+future\s*)?(?:can|could|would|will)\s+you\s+", "", text, flags=re.I)
    text = re.sub(r"^please\s+", "", text, flags=re.I)

    patterns = [
        r"\bset\s+(?:a\s+)?reminder\s+(?:to|for|about)\s+(?P<title>.+)$",
        r"\bremind\s+(?:me\s+)?(?:to|about)\s+(?P<title>.+)$",
        r"\b(?:schedule|create|add|make|plan)\s+(?P<title>.+)$",
        r"\b(?:appointment|event)\s+(?:for\s+)?(?P<title>.+)$",
    ]

    title = text
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            title = match.group("title").strip()
            break

    title = re.sub(r"^(?:an?\s+)?(?:event|appointment|reminder)\s+(?:to|for|about)?\s*", "", title, flags=re.I)
    title = re.sub(r"^(?:for|called)\s+", "", title, flags=re.I)

    weekday = r"monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    num_time = r"\d{1,2}(?::\d{2})?\s*(?:am|pm)?"
    word_time = r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
    title = re.sub(
        rf"\s+\b(?:at|on|until|from|to|by)\b\s+(?:{num_time}|{word_time}(?:\s*(?:am|pm))?|tomorrow|today|tonight|{weekday}|this\s+week|next\s+week)\b.*$",
        "",
        title,
        flags=re.I,
    )
    title = re.sub(
        rf"\s+\b(?:tomorrow|today|tonight|{weekday}|this\s+(?:morning|afternoon|evening|week)|next\s+(?:morning|afternoon|evening|week))\b.*$",
        "",
        title,
        flags=re.I,
    )
    title = re.sub(r"\s+please$", "", title, flags=re.I).strip(" ,.-")
    return title or "New event"


def _calendar_range_from_command(command_lower):
    command_lower = command_lower.replace("calender", "calendar")
    command_lower = command_lower.replace("tmrw", "tomorrow")
    command_lower = command_lower.replace("tmr", "tomorrow")
    if "tomorrow" in command_lower:
        return "tomorrow"
    if any(token in command_lower for token in ["this month", "next month", "month"]):
        return "month"
    if any(token in command_lower for token in ["next week", "this week", "next 7", "next seven", "upcoming"]):
        return "week"
    return "today"


def _is_calendar_create_intent(command_lower):
    command_lower = command_lower.replace("calender", "calendar")
    command_lower = command_lower.replace("tmrw", "tomorrow")
    command_lower = command_lower.replace("tmr", "tomorrow")
    if re.search(r"\b(?:schedule|create|add|make|plan)\b", command_lower):
        return True
    if re.search(r"\bset\s+(?:a\s+)?reminder\b", command_lower):
        return True
    if re.search(r"\bremind\s+(?:me\s+)?\b", command_lower):
        return True
    if re.search(r"\b(?:create|schedule|add|make|plan|set)\b", command_lower) and re.search(r"\b(?:event|appointment)\b", command_lower):
        return True
    return False


def _is_calendar_lookup_intent(command_lower):
    command_lower = command_lower.replace("calender", "calendar")
    command_lower = command_lower.replace("tmrw", "tomorrow")
    command_lower = command_lower.replace("tmr", "tomorrow")

    if "open" in command_lower and "calendar" in command_lower:
        return False

    lookup_phrases = [
        "what do i have",
        "what's on",
        "whats on",
        "do i have anything",
        "going on today",
        "my schedule",
        "on my calendar",
        "calendar today",
        "calendar tomorrow",
        "show calendar",
        "show my calendar",
        "check calendar",
        "check my calendar",
        "list calendar",
        "list events",
        "show events",
        "upcoming events",
        "this month",
        "next month",
        "am i busy",
    ]
    return any(phrase in command_lower for phrase in lookup_phrases)


def _format_calendar_list_reply(events, range_name):
    window_label = {
        "today": "today",
        "tomorrow": "tomorrow",
        "week": "this week",
        "month": "this month",
    }.get(range_name, "today")

    if not events:
        return f"You have nothing scheduled {window_label}."

    lines = []
    for item in events[:6]:
        time_text = str(item.get("time", "Time unknown")).strip() or "Time unknown"
        title_text = str(item.get("title", "(untitled event)")).strip() or "(untitled event)"
        subtitle_text = str(item.get("subtitle", "")).strip()
        if subtitle_text:
            lines.append(f"{time_text} - {title_text} ({subtitle_text})")
        else:
            lines.append(f"{time_text} - {title_text}")

    return f"Here is your schedule {window_label}: " + "; ".join(lines)


def _format_created_event_confirmation(event_payload, fallback_title, fallback_start=None, fallback_end=None):
    title = str(event_payload.get("summary") or fallback_title or "New event").strip()

    start_info = event_payload.get("start", {}) if isinstance(event_payload, dict) else {}
    end_info = event_payload.get("end", {}) if isinstance(event_payload, dict) else {}
    start_raw = start_info.get("dateTime") or start_info.get("date") or fallback_start
    end_raw = end_info.get("dateTime") or end_info.get("date") or fallback_end

    when_text = "time not set"
    if start_raw:
        try:
            start_dt = datetime.datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
            local_start = start_dt.astimezone()
            start_label = local_start.strftime("%A, %b %d at %I:%M %p").replace(" 0", " ")
            if end_raw:
                end_dt = datetime.datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
                local_end = end_dt.astimezone()
                end_label = local_end.strftime("%I:%M %p").lstrip("0")
                when_text = f"{start_label} to {end_label}"
            else:
                when_text = start_label
        except Exception:
            when_text = str(start_raw)

    location = ""
    if isinstance(event_payload, dict):
        location = str(event_payload.get("location") or "").strip()

    if location:
        return f"Added to your calendar: {title}. Scheduled for {when_text}. Location: {location}."
    return f"Added to your calendar: {title}. Scheduled for {when_text}."


def _calendar_followup_cancelled(command_lower):
    normalized = command_lower.strip()
    return normalized in {"cancel", "never mind", "nevermind", "stop", "dont", "don't"}


def has_pending_calendar_draft():
    return PENDING_CALENDAR_DRAFT is not None


def clear_pending_calendar_draft():
    global PENDING_CALENDAR_DRAFT
    PENDING_CALENDAR_DRAFT = None


def should_handle_calendar_followup(command):
    if not has_pending_calendar_draft():
        return False
    command_lower = (command or "").lower().replace("tmrw", "tomorrow").replace("tmr", "tomorrow")
    if _calendar_followup_cancelled(command_lower):
        return True
    start_time, _ = parse_calendar_time(command or "")
    return bool(start_time)


def handle_calendar_command(command):
    global PENDING_CALENDAR_DRAFT
    command_lower = command.lower()
    command_lower = command_lower.replace("calender", "calendar")
    command_lower = command_lower.replace("tmrw", "tomorrow")
    command_lower = command_lower.replace("tmr", "tomorrow")

    if PENDING_CALENDAR_DRAFT and _calendar_followup_cancelled(command_lower):
        PENDING_CALENDAR_DRAFT = None
        return "Okay, canceled that calendar draft."

    if PENDING_CALENDAR_DRAFT and not _is_calendar_lookup_intent(command_lower):
        followup_start, followup_end = parse_calendar_time(command)
        if followup_start:
            draft = PENDING_CALENDAR_DRAFT
            PENDING_CALENDAR_DRAFT = None
            title = str(draft.get("title") or "New event").strip() or "New event"
            details = "Created from Future"
            try:
                load_google_calendar_tokens_into_globals()
                created_event = create_google_calendar_event(title, details, followup_start, followup_end)
            except RuntimeError as exc:
                if "access token" in str(exc).lower() or "refresh token" in str(exc).lower():
                    return request_google_calendar_authorization("Creating calendar events needs your Google account connected.")
                return str(exc)
            except Exception:
                return request_google_calendar_authorization("Creating calendar events needs your Google account connected.")

            return _format_created_event_confirmation(created_event, title, followup_start, followup_end)

    if _is_calendar_create_intent(command_lower):
        title = _normalize_calendar_title(command)
        details = "Created from Future"
        start_time, end_time = parse_calendar_time(command)

        try:
            load_google_calendar_tokens_into_globals()
            if not start_time:
                PENDING_CALENDAR_DRAFT = {
                    "command": command,
                    "title": title,
                }
                return "I can create that, but I still need a time. Reply with just a time like 12pm, or say: schedule dentist tomorrow at 3pm."
            created_event = create_google_calendar_event(title, details, start_time, end_time)
        except RuntimeError as exc:
            if "access token" in str(exc).lower() or "refresh token" in str(exc).lower():
                return request_google_calendar_authorization("Creating calendar events needs your Google account connected.")
            return str(exc)
        except Exception:
            return request_google_calendar_authorization("Creating calendar events needs your Google account connected.")

        PENDING_CALENDAR_DRAFT = None
        return _format_created_event_confirmation(created_event, title, start_time, end_time)

    if _is_calendar_lookup_intent(command_lower):
        range_name = _calendar_range_from_command(command_lower)
        try:
            events = list_google_calendar_events(range_name=range_name, max_results=8)
            return _format_calendar_list_reply(events, range_name)
        except RuntimeError as exc:
            if "access token" in str(exc).lower() or "refresh token" in str(exc).lower():
                return request_google_calendar_authorization("Checking your calendar needs your Google account connected.")
            return str(exc)
        except Exception:
            return request_google_calendar_authorization("Checking your calendar needs your Google account connected.")

    if "open" in command_lower and "calendar" in command_lower:
        webbrowser.open("https://calendar.google.com")
        return "Opening Google Calendar."

    return "I can manage your calendar. Try: schedule gym today at 7pm."


def _normalize_email_address(raw_address):
    cleaned = raw_address.strip().lower()
    cleaned = re.sub(r"\s+at\s+", "@", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+dot\s+", ".", cleaned, flags=re.I)
    cleaned = cleaned.replace(" ", "")
    return cleaned


def _load_email_contacts(path=None):
    contact_path = path or EMAIL_CONTACTS_FILE
    if not os.path.exists(contact_path):
        return {}

    try:
        with open(contact_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}

    if not isinstance(data, dict):
        return {}

    normalized = {}
    for name, email in data.items():
        if not isinstance(name, str) or not isinstance(email, str):
            continue
        normalized_email = _normalize_email_address(email)
        if re.match(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", normalized_email):
            normalized[name.strip().lower()] = normalized_email
    return normalized


def _extract_contact_name(command):
    patterns = [
        r"\b(?:send\s+)?(?:email|mail|message|write)\s+(?:to\s+)?([A-Za-z][A-Za-z0-9_-]*)\b",
        r"\bto\s+([A-Za-z][A-Za-z0-9_-]*)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, command, flags=re.I)
        if not match:
            continue
        name = match.group(1).strip()
        if "@" in name:
            continue
        return name
    return None


def _is_affirmative(text):
    normalized = text.strip().lower()
    return normalized in {
        "yes", "y", "yeah", "yep", "confirm", "send", "send it", "do it", "go ahead", "correct"
    }


def _is_negative(text):
    normalized = text.strip().lower()
    return normalized in {"no", "n", "nope", "cancel", "stop", "dont send", "don't send"}


def has_pending_gmail_confirmation():
    return PENDING_GMAIL_CONFIRMATION is not None


def clear_pending_gmail_confirmation():
    global PENDING_GMAIL_CONFIRMATION
    PENDING_GMAIL_CONFIRMATION = None


def _gmail_headers():
    load_google_calendar_tokens_into_globals()
    if not GOOGLE_CALENDAR_ACCESS_TOKEN and not GOOGLE_CALENDAR_REFRESH_TOKEN:
        raise RuntimeError("Google access token not available")
    if not GOOGLE_CALENDAR_ACCESS_TOKEN and GOOGLE_CALENDAR_REFRESH_TOKEN:
        _refresh_google_calendar_access_token()
    return {"Authorization": f"Bearer {GOOGLE_CALENDAR_ACCESS_TOKEN}"}


def list_gmail_messages(max_results=5, query="in:inbox"):
    headers = _gmail_headers()
    response = requests.get(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages",
        headers=headers,
        params={"maxResults": max_results, "q": query},
        timeout=20,
    )
    if response.status_code == 401 and GOOGLE_CALENDAR_REFRESH_TOKEN:
        _refresh_google_calendar_access_token()
        headers = _gmail_headers()
        response = requests.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            headers=headers,
            params={"maxResults": max_results, "q": query},
            timeout=20,
        )
    response.raise_for_status()
    message_ids = response.json().get("messages", [])

    results = []
    for item in message_ids:
        message_id = item.get("id")
        if not message_id:
            continue
        detail = requests.get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}",
            headers=headers,
            params={"format": "metadata", "metadataHeaders": ["Subject", "From", "Date"]},
            timeout=20,
        )
        if detail.status_code == 401 and GOOGLE_CALENDAR_REFRESH_TOKEN:
            _refresh_google_calendar_access_token()
            headers = _gmail_headers()
            detail = requests.get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}",
                headers=headers,
                params={"format": "metadata", "metadataHeaders": ["Subject", "From", "Date"]},
                timeout=20,
            )
        detail.raise_for_status()
        payload = detail.json().get("payload", {})
        headers_list = payload.get("headers", [])
        header_map = {h.get("name", "").lower(): h.get("value", "") for h in headers_list}
        results.append(
            {
                "id": message_id,
                "from": header_map.get("from", "unknown"),
                "subject": header_map.get("subject", "(no subject)"),
                "date": header_map.get("date", ""),
                "snippet": detail.json().get("snippet", ""),
            }
        )
    return results


def send_gmail_message(to_email, subject, body):
    headers = _gmail_headers()
    message = MIMEText(body)
    message["to"] = to_email
    message["subject"] = subject
    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    response = requests.post(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        headers=headers,
        json={"raw": raw_message},
        timeout=20,
    )
    if response.status_code == 401 and GOOGLE_CALENDAR_REFRESH_TOKEN:
        _refresh_google_calendar_access_token()
        headers = _gmail_headers()
        response = requests.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers=headers,
            json={"raw": raw_message},
            timeout=20,
        )
    response.raise_for_status()
    return response.json()


def send_gmail_message_with_attachments(to_email, subject, body, attachments=None):
    """Same as send_gmail_message but with optional [(filename, bytes, mime_type), ...] attachments."""
    headers = _gmail_headers()
    message = MIMEMultipart()
    message["to"] = to_email
    message["subject"] = subject
    message.attach(MIMEText(body))

    for filename, file_bytes, mime_type in attachments or []:
        maintype, _, subtype = (mime_type or "application/octet-stream").partition("/")
        part = MIMEBase(maintype or "application", subtype or "octet-stream")
        part.set_payload(file_bytes)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
        message.attach(part)

    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    payload = {"raw": raw_message}
    response = requests.post(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        headers=headers,
        json=payload,
        timeout=60,
    )
    if response.status_code == 401 and GOOGLE_CALENDAR_REFRESH_TOKEN:
        _refresh_google_calendar_access_token()
        headers = _gmail_headers()
        response = requests.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers=headers,
            json=payload,
            timeout=60,
        )
    response.raise_for_status()
    return response.json()


def _decode_gmail_part_body(part):
    data = (part.get("body", {}) or {}).get("data")
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="ignore")
    except Exception:
        return ""


def get_gmail_message_body(message_id):
    """Fetch a message's plain-text body (falls back to the snippet) for parsing replies."""
    headers = _gmail_headers()
    response = requests.get(
        f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}",
        headers=headers,
        params={"format": "full"},
        timeout=20,
    )
    if response.status_code == 401 and GOOGLE_CALENDAR_REFRESH_TOKEN:
        _refresh_google_calendar_access_token()
        headers = _gmail_headers()
        response = requests.get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}",
            headers=headers,
            params={"format": "full"},
            timeout=20,
        )
    response.raise_for_status()
    payload = response.json().get("payload", {}) or {}

    def _walk(node):
        mime_type = node.get("mimeType", "")
        if mime_type == "text/plain":
            text = _decode_gmail_part_body(node)
            if text:
                return text
        for child in node.get("parts", []) or []:
            text = _walk(child)
            if text:
                return text
        return ""

    body_text = _walk(payload) or _decode_gmail_part_body(payload)
    if body_text:
        return body_text
    return response.json().get("snippet", "")


def _extract_gmail_send_parts(command):
    to_match = re.search(
        r"\bto\s+([A-Za-z0-9._%+-]+(?:\s*(?:@|at)\s*)[A-Za-z0-9.-]+(?:\s*(?:\.|dot)\s*)[A-Za-z]{2,})",
        command,
        flags=re.I,
    )
    to_email = _normalize_email_address(to_match.group(1)) if to_match else None
    if to_email and not re.match(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", to_email):
        to_email = None

    subject_match = re.search(r"\bsubject\s+(.+?)(?:\s+body\s+|\s+message\s+|$)", command, flags=re.I)
    body_match = re.search(r"\b(?:body|message)\s+(.+)$", command, flags=re.I)

    if not subject_match:
        subject_match = re.search(r"\babout\s+(.+?)(?:\s+(?:that|saying)\s+|$)", command, flags=re.I)
    if not body_match:
        body_match = re.search(r"\b(?:that|saying)\s+(.+)$", command, flags=re.I)

    subject_value = subject_match.group(1).strip() if subject_match else "Future message"
    body_value = body_match.group(1).strip() if body_match else "Sent from Future."

    if subject_value.lower().startswith("to "):
        subject_value = "Future message"

    return {
        "to": to_email,
        "subject": subject_value,
        "body": body_value,
    }


def handle_gmail_command(command):
    global PENDING_GMAIL_CONFIRMATION
    command_lower = command.lower()
    try:
        load_google_calendar_tokens_into_globals()
        if not GOOGLE_CALENDAR_ACCESS_TOKEN and not GOOGLE_CALENDAR_REFRESH_TOKEN:
            return request_google_gmail_authorization("Gmail needs your Google account connected.")

        if PENDING_GMAIL_CONFIRMATION:
            if _is_affirmative(command):
                pending = PENDING_GMAIL_CONFIRMATION
                PENDING_GMAIL_CONFIRMATION = None
                send_gmail_message(pending["to"], pending["subject"], pending["body"])
                return f"Gmail sent to {pending['to']} with subject '{pending['subject']}'."
            if _is_negative(command):
                pending = PENDING_GMAIL_CONFIRMATION
                PENDING_GMAIL_CONFIRMATION = None
                return f"Okay, canceled the email to {pending['to']}."

        should_send = any(phrase in command_lower for phrase in [
            "send", "email to", "mail to", "write to", "message to"
        ]) or bool(re.search(r"^\s*(email|mail|write|message)\b", command_lower))
        if should_send:
            parts = _extract_gmail_send_parts(command)
            if not parts["to"]:
                contact_name = _extract_contact_name(command)
                if contact_name:
                    contacts = _load_email_contacts()
                    resolved_email = contacts.get(contact_name.lower())
                    if resolved_email:
                        PENDING_GMAIL_CONFIRMATION = {
                            "to": resolved_email,
                            "subject": parts["subject"],
                            "body": parts["body"],
                            "name": contact_name,
                        }
                        return (
                            f"I found {contact_name} as {resolved_email}. "
                            "Should I send it now?"
                        )

                return "To send Gmail, include an address like: send email to name@example.com subject Hello message How are you"
            send_gmail_message(parts["to"], parts["subject"], parts["body"])
            return f"Gmail sent to {parts['to']} with subject '{parts['subject']}'."

        query = "is:unread" if "unread" in command_lower else "in:inbox"
        messages = list_gmail_messages(max_results=5, query=query)
        if not messages:
            return "No Gmail messages found for that query."

        summary = " | ".join(
            f"{index + 1}. From: {msg['from']}; Subject: {msg['subject']}"
            for index, msg in enumerate(messages)
        )
        return f"Here are your latest Gmail messages: {summary}"
    except requests.HTTPError as exc:
        response = getattr(exc, "response", None)
        if response is not None and response.status_code in {401, 403}:
            return request_google_gmail_authorization(
                "Gmail access needs updated Google permissions."
            )
        return f"Gmail request failed: {exc}"
    except RuntimeError as exc:
        if "access token" in str(exc).lower() or "refresh token" in str(exc).lower():
            return request_google_gmail_authorization("Gmail needs your Google account connected.")
        return str(exc)

