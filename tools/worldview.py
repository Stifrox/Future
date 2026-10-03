"""World view: live public cameras, aircraft, satellites, quakes, launches and fires for Future."""
import csv
import io
import json
import math
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

try:
    from sgp4.api import Satrec, jday
except Exception:  # sgp4 is optional; satellites degrade to unavailable
    Satrec = None
    jday = None

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "world_cache"
CATALOG_FILES = [DATA_DIR / "cctv_catalogs.json", DATA_DIR / "cctv_catalogs_extra.json"]
UA = {"User-Agent": "Mozilla/5.0 (Future-WorldView)"}
CAMERA_TTL = 12 * 3600
MAX_FRAME_BYTES = 6 * 1024 * 1024

_ttl_cache: Dict[str, Tuple[float, Any]] = {}
_lock = threading.Lock()
_cameras: List[Dict[str, Any]] = []
_cameras_by_id: Dict[str, Dict[str, Any]] = {}
_catalog_meta: Dict[str, Dict[str, Any]] = {}
_cameras_loaded_at = 0.0
_geo_lock = threading.Lock()
_geo_last = 0.0


def _cached(key: str, ttl: float, fn):
    now = time.time()
    with _lock:
        hit = _ttl_cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = fn()
    with _lock:
        _ttl_cache[key] = (time.time(), value)
    return value


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


# ---------------------------------------------------------------- geometry

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(a)))


def _in_bbox(lat: float, lon: float, bbox: Tuple[float, float, float, float]) -> bool:
    s, w, n, e = bbox
    if not (s <= lat <= n):
        return False
    return (w <= lon <= e) if w <= e else (lon >= w or lon <= e)


# ---------------------------------------------------------------- geocoding

def geocode(query: str) -> Optional[Dict[str, Any]]:
    """Resolve a place name to center + bbox via Nominatim (cached, 1 req/sec)."""
    global _geo_last
    query = (query or "").strip()
    if not query:
        return None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / "geocode.json"
    store: Dict[str, Any] = {}
    try:
        store = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        store = {}
    key = query.lower()
    if key in store:
        return store[key] or None
    with _geo_lock:
        wait = 1.1 - (time.time() - _geo_last)
        if wait > 0:
            time.sleep(wait)
        try:
            resp = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": query, "format": "jsonv2", "limit": 1},
                headers=UA,
                timeout=15,
            )
            _geo_last = time.time()
            rows = resp.json() if resp.ok else []
        except Exception:
            return None
    if not rows:
        store[key] = None
    else:
        row = rows[0]
        bb = [float(x) for x in row.get("boundingbox", [])] or None
        store[key] = {
            "name": row.get("display_name", query),
            "lat": float(row["lat"]),
            "lon": float(row["lon"]),
            "bbox": [bb[0], bb[2], bb[1], bb[3]] if bb else None,  # s, w, n, e
        }
    try:
        path.write_text(json.dumps(store), encoding="utf-8")
    except Exception:
        pass
    return store[key]


# ---------------------------------------------------------------- camera catalogs

def _dig(obj: Any, path: Optional[str]) -> Any:
    if path in (None, ""):
        return obj
    cur = obj
    for part in str(path).split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _num(value: Any) -> Optional[float]:
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _wkt_point(value: Any) -> Optional[Tuple[float, float]]:
    match = re.search(r"POINT\s*\(\s*(-?[\d.]+)\s+(-?[\d.]+)", str(value or ""), re.I)
    return (float(match.group(2)), float(match.group(1))) if match else None


def _xml_records(text: str, tag: str) -> List[Dict[str, Any]]:
    root = ET.fromstring(text)
    out = []
    for el in root.iter():
        if el.tag.split("}")[-1] == tag:
            out.append({c.tag.split("}")[-1]: (c.text or "").strip() for c in el})
    return out


def _records_from_response(cat: Dict[str, Any], resp: requests.Response) -> List[Any]:
    if cat.get("format") == "xml":
        return _xml_records(resp.text, cat.get("recordTag", ""))
    data = resp.json()
    rows = _dig(data, cat.get("arrayPath"))
    return rows if isinstance(rows, list) else []


def _build_camera(cat: Dict[str, Any], row: Any) -> Optional[Dict[str, Any]]:
    f = cat.get("fields", {})
    raw_id = _dig(row, f.get("id"))
    if raw_id in (None, ""):
        return None
    raw_id = str(raw_id)
    if cat.get("idStripPrefix") and raw_id.startswith(cat["idStripPrefix"]):
        raw_id = raw_id[len(cat["idStripPrefix"]):]
    lat = _num(_dig(row, f.get("lat")))
    lon = _num(_dig(row, f.get("lon")))
    if (lat is None or lon is None) and f.get("wkt"):
        point = _wkt_point(_dig(row, f.get("wkt")))
        if point:
            lat, lon = point
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    url = _dig(row, f.get("imageUrl")) if f.get("imageUrl") else None
    if url and cat.get("imageUrlRegex"):
        match = re.search(cat["imageUrlRegex"], str(url))
        url = match.group(1) if match else None
    if not url and cat.get("imageUrlTemplate"):
        url = cat["imageUrlTemplate"].replace("{id}", raw_id)
    if not url:
        return None
    url = str(url).strip()
    if not url.lower().startswith("http"):
        prefix = cat.get("imageUrlPrefix") or cat.get("imageBaseUrl") or ""
        url = prefix + url
    if not url.lower().startswith(("http://", "https://")):
        return None
    name = _dig(row, f.get("name")) if f.get("name") else None
    heading = _dig(row, f.get("heading")) if f.get("heading") else None
    stream = _dig(row, cat.get("streamUrl")) if cat.get("streamUrl") else None
    if not stream and cat.get("streamUrlTemplate"):
        stream = cat["streamUrlTemplate"].replace("{id}", raw_id)
    video = cat["videoUrlTemplate"].replace("{id}", raw_id) if cat.get("videoUrlTemplate") else None
    cam = {
        "id": f"{cat['id']}:{raw_id}",
        "name": re.sub(r"\s+", " ", str(name or raw_id)).strip()[:120],
        "lat": round(lat, 5),
        "lon": round(lon, 5),
        "heading": str(heading) if heading not in (None, "") else "",
        "provider": cat.get("provider", cat["id"]),
        "country": cat.get("country", ""),
        "region": cat.get("countryName", ""),
        "url": url,
    }
    if stream and str(stream).lower().startswith("https://"):
        cam["stream"] = str(stream)
    if video and video.lower().startswith("https://"):
        cam["video"] = video
    return cam


def _load_catalog(cat: Dict[str, Any]) -> List[Dict[str, Any]]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"cameras2_{cat['id']}.json"
    try:
        if time.time() - cache_file.stat().st_mtime < CAMERA_TTL:
            return json.loads(cache_file.read_text(encoding="utf-8"))
    except Exception:
        pass
    headers = {**UA, **(cat.get("headers") or {})}
    try:
        if cat.get("method", "GET").upper() == "POST":
            resp = requests.post(cat["catalogUrl"], data=cat.get("body"), headers=headers, timeout=25)
        else:
            resp = requests.get(cat["catalogUrl"], headers=headers, timeout=25)
        resp.raise_for_status()
        cams = [c for c in (_build_camera(cat, r) for r in _records_from_response(cat, resp)) if c]
    except Exception as exc:
        _catalog_meta[cat["id"]] = {"ok": False, "error": str(exc)[:120]}
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))  # stale beats nothing
        except Exception:
            return []
    try:
        cache_file.write_text(json.dumps(cams), encoding="utf-8")
    except Exception:
        pass
    return cams


def load_cameras(force: bool = False) -> List[Dict[str, Any]]:
    """Load every enabled catalog in parallel; cached on disk for 12h."""
    global _cameras, _cameras_by_id, _cameras_loaded_at
    with _lock:
        if _cameras and not force and time.time() - _cameras_loaded_at < CAMERA_TTL:
            return _cameras
    cats: List[Dict[str, Any]] = []
    for file in CATALOG_FILES:
        try:
            cats.extend(json.loads(file.read_text(encoding="utf-8")))
        except Exception:
            continue
    cats = [c for c in cats if c.get("enabled", True)]
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(_load_catalog, cats))
    merged: List[Dict[str, Any]] = []
    for cat, cams in zip(cats, results):
        _catalog_meta.setdefault(cat["id"], {})
        _catalog_meta[cat["id"]].update({"ok": bool(cams), "count": len(cams), "provider": cat.get("provider", "")})
        merged.extend(cams)
    with _lock:
        _cameras = merged
        _cameras_by_id = {c["id"]: c for c in merged}
        _cameras_loaded_at = time.time()
    return merged


def get_camera(cam_id: str) -> Optional[Dict[str, Any]]:
    load_cameras()
    return _cameras_by_id.get(cam_id)


def _public_camera(cam: Dict[str, Any], dist: Optional[float] = None) -> Dict[str, Any]:
    out = {k: cam[k] for k in ("id", "name", "lat", "lon", "heading", "provider", "region")}
    for key in ("stream", "video"):
        if cam.get(key):
            out[key] = cam[key]
    if dist is not None:
        out["distance_km"] = round(dist, 1)
    return out


def search_cameras(
    place: Optional[str] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    radius_km: float = 75,
    bbox: Optional[Tuple[float, float, float, float]] = None,
    limit: int = 60,
    text: Optional[str] = None,
) -> Dict[str, Any]:
    cams = load_cameras()
    center: Optional[Tuple[float, float]] = (lat, lon) if lat is not None and lon is not None else None
    label = place or ""
    if place and not bbox:
        geo = geocode(place)
        if not geo:
            return {"query": place, "center": None, "bbox": None, "count": 0, "cameras": [], "error": "place not found"}
        label = geo["name"]
        center = (geo["lat"], geo["lon"])
        if geo.get("bbox"):
            s, w, n, e = geo["bbox"]
            # Tiny boxes (a city center point) get a usable minimum radius.
            if haversine_km(s, w, n, e) < radius_km * 1.2:
                bbox = None
            else:
                bbox = (s, w, n, e)
    if bbox:
        pool = [c for c in cams if _in_bbox(c["lat"], c["lon"], bbox)]
        if center is None:
            center = ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)
    elif center:
        pool = [c for c in cams if haversine_km(center[0], center[1], c["lat"], c["lon"]) <= radius_km]
        if not pool:  # widen once so a quiet area still returns the nearest feeds
            pool = [c for c in cams if haversine_km(center[0], center[1], c["lat"], c["lon"]) <= radius_km * 5]
    else:
        pool = cams
    if text:
        needle = text.lower()
        pool = [c for c in pool if needle in c["name"].lower()]
    if center:
        scored = sorted(((haversine_km(center[0], center[1], c["lat"], c["lon"]), c) for c in pool), key=lambda t: t[0])
        results = [_public_camera(c, d) for d, c in scored[:limit]]
    else:
        results = [_public_camera(c) for c in pool[:limit]]
    return {
        "query": label,
        "center": list(center) if center else None,
        "bbox": list(bbox) if bbox else None,
        "count": len(pool),
        "cameras": results,
    }


def _frame_headers(cam_id: str) -> Dict[str, str]:
    headers = dict(UA)
    for file in CATALOG_FILES:
        try:
            for cat in json.loads(file.read_text(encoding="utf-8")):
                if cat["id"] == cam_id.split(":", 1)[0]:
                    headers.update(cat.get("headers") or {})
        except Exception:
            continue
    return headers


def _read_image(resp: requests.Response) -> Tuple[bytes, str]:
    resp.raise_for_status()
    ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
    body = b""
    for chunk in resp.iter_content(65536):
        body += chunk
        if len(body) > MAX_FRAME_BYTES:
            raise ValueError("frame too large")
    if not ctype.startswith("image/"):
        if body[:3] == b"\xff\xd8\xff":
            ctype = "image/jpeg"
        elif body[:4] == b"\x89PNG":
            ctype = "image/png"
        else:
            raise ValueError("camera did not return an image")
    return body, ctype


def fetch_camera_frame(cam_id: str) -> Tuple[bytes, str]:
    """Fetch the current still for a catalogued camera. Only catalogued URLs are ever requested."""
    cam = get_camera(cam_id)
    if not cam:
        raise LookupError("camera not found")
    return _read_image(requests.get(cam["url"], headers=_frame_headers(cam_id), timeout=15, stream=True))


_frame_cache: Dict[str, Tuple[float, bytes, str, str]] = {}


def get_frame_cached(cam_id: str, max_age: float = 1.0) -> Tuple[bytes, str, str]:
    """Latest still plus an ETag; fetches are shared for max_age seconds so many viewers cost one upstream request."""
    import hashlib

    now = time.time()
    with _lock:
        hit = _frame_cache.get(cam_id)
    if hit and now - hit[0] < max_age:
        return hit[1], hit[2], hit[3]
    body, ctype = fetch_camera_frame(cam_id)
    etag = '"' + hashlib.md5(body).hexdigest() + '"'
    with _lock:
        _frame_cache[cam_id] = (time.time(), body, ctype, etag)
        if len(_frame_cache) > 200:
            for key in sorted(_frame_cache, key=lambda k: _frame_cache[k][0])[:100]:
                _frame_cache.pop(key, None)
    return body, ctype, etag


def describe_camera(cam_id: str, question: str = "") -> str:
    import base64

    from tools.vision_tool import analyze_visual_frames

    cam = get_camera(cam_id)
    if not cam:
        return "Camera not found."
    body, ctype = fetch_camera_frame(cam_id)
    data_url = f"data:{ctype};base64,{base64.b64encode(body).decode()}"
    prompt = question.strip() or "What is happening here? Describe weather, traffic and conditions."
    system = (
        "You are Future looking through a live public camera. Describe exactly what is visible: "
        "weather, road and traffic conditions, visibility, crowds, notable activity. Be concise and direct."
    )
    return analyze_visual_frames([data_url], user_prompt=f"{prompt} (Camera: {cam['name']}, {cam['region']})", custom_system_prompt=system)


# ---------------------------------------------------------------- aircraft

def get_aircraft(lat: float, lon: float, radius_nm: float = 150, military: bool = False) -> Dict[str, Any]:
    radius_nm = max(5, min(250, radius_nm))

    def _fetch():
        url = "https://api.adsb.lol/v2/mil" if military else f"https://api.adsb.lol/v2/point/{lat:.4f}/{lon:.4f}/{radius_nm:.0f}"
        resp = requests.get(url, headers=UA, timeout=20)
        resp.raise_for_status()
        return resp.json().get("ac", [])

    key = f"ac:{military}:{round(lat, 1)}:{round(lon, 1)}:{int(radius_nm)}"
    try:
        rows = _cached(key, 15, _fetch)
    except Exception as exc:
        fallback = _opensky_aircraft(lat, lon, radius_nm) if not military else None
        if fallback is not None:
            return fallback
        return {"count": 0, "aircraft": [], "error": str(exc)[:120]}
    out = []
    for r in rows:
        if r.get("lat") is None or r.get("lon") is None:
            continue
        alt = r.get("alt_baro")
        out.append({
            "id": r.get("hex", ""),
            "callsign": (r.get("flight") or "").strip(),
            "reg": r.get("r", ""),
            "type": r.get("t", ""),
            "lat": r["lat"],
            "lon": r["lon"],
            "alt_ft": alt if isinstance(alt, (int, float)) else 0,
            "speed_kt": r.get("gs"),
            "heading": r.get("track"),
            "squawk": r.get("squawk", ""),
            "military": bool(int(r.get("dbFlags", 0) or 0) & 1),
        })
    if military:
        out = [a for a in out if haversine_km(lat, lon, a["lat"], a["lon"]) <= radius_nm * 1.852]
    out = out[:400]
    return {"count": len(out), "aircraft": out}


# ---------------------------------------------------------------- earthquakes

QUAKE_FEEDS = {"all_hour", "all_day", "2.5_day", "2.5_week", "4.5_day", "4.5_week", "4.5_month", "significant_week"}


def get_quakes(feed: str = "2.5_day", min_mag: float = 0) -> Dict[str, Any]:
    if feed not in QUAKE_FEEDS:
        feed = "2.5_day"

    def _fetch():
        resp = requests.get(f"https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/{feed}.geojson", headers=UA, timeout=20)
        resp.raise_for_status()
        return resp.json().get("features", [])

    try:
        rows = _cached(f"quake:{feed}", 300, _fetch)
    except Exception as exc:
        return {"count": 0, "quakes": [], "error": str(exc)[:120]}
    out = []
    for f in rows:
        p, g = f.get("properties", {}), f.get("geometry", {}).get("coordinates", [None, None, None])
        mag = p.get("mag")
        if mag is None or mag < min_mag or g[0] is None:
            continue
        out.append({
            "id": f.get("id", ""),
            "mag": mag,
            "place": p.get("place", ""),
            "time": datetime.fromtimestamp(p["time"] / 1000, tz=timezone.utc).isoformat(timespec="minutes"),
            "lat": g[1],
            "lon": g[0],
            "depth_km": g[2],
            "url": p.get("url", ""),
            "tsunami": bool(p.get("tsunami")),
        })
    out.sort(key=lambda q: q["time"], reverse=True)
    return {"count": len(out), "quakes": out[:500]}


# ---------------------------------------------------------------- satellites

SAT_GROUPS = {"stations", "visual", "weather", "gps-ops", "geo", "science", "starlink", "active"}


def _tle_text(group: str) -> str:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"tle_{group}.txt"
    try:
        if time.time() - cache_file.stat().st_mtime < 2 * 3600:
            return cache_file.read_text(encoding="utf-8")
    except Exception:
        pass
    try:
        resp = requests.get("https://celestrak.org/NORAD/elements/gp.php", params={"GROUP": group, "FORMAT": "tle"}, headers=UA, timeout=25)
        resp.raise_for_status()
        cache_file.write_text(resp.text, encoding="utf-8")
        return resp.text
    except Exception:
        return cache_file.read_text(encoding="utf-8") if cache_file.exists() else ""


def _teme_to_geodetic(r: Tuple[float, float, float], jd: float, fr: float) -> Tuple[float, float, float]:
    t = (jd - 2451545.0) + fr
    gmst = math.radians((280.46061837 + 360.98564736629 * t) % 360.0)
    x, y, z = r
    lon = (math.atan2(y, x) - gmst + math.pi) % (2 * math.pi) - math.pi
    a, f = 6378.137, 1 / 298.257223563
    e2 = f * (2 - f)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - e2))
    alt = 0.0
    for _ in range(5):
        n = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        alt = p / math.cos(lat) - n if abs(math.cos(lat)) > 1e-9 else abs(z) / math.sin(lat) - n * (1 - e2)
        lat = math.atan2(z, p * (1 - e2 * n / (n + alt)))
    return math.degrees(lat), math.degrees(lon), alt


def get_satellites(group: str = "stations", name: str = "", limit: int = 300) -> Dict[str, Any]:
    if Satrec is None:
        return {"count": 0, "satellites": [], "error": "sgp4 not installed"}
    if group not in SAT_GROUPS:
        group = "stations"
    lines = [ln.rstrip() for ln in _tle_text(group).splitlines() if ln.strip()]
    now = datetime.now(timezone.utc)
    jd, fr = jday(now.year, now.month, now.day, now.hour, now.minute, now.second + now.microsecond / 1e6)
    out = []
    needle = name.lower()
    for i in range(0, len(lines) - 2, 3):
        sat_name, l1, l2 = lines[i].strip(), lines[i + 1], lines[i + 2]
        if needle and needle not in sat_name.lower():
            continue
        try:
            sat = Satrec.twoline2rv(l1, l2)
            err, pos, vel = sat.sgp4(jd, fr)
        except Exception:
            continue
        if err != 0:
            continue
        lat, lon, alt = _teme_to_geodetic(pos, jd, fr)
        out.append({
            "id": l1[2:7].strip(),
            "name": sat_name,
            "lat": round(lat, 4),
            "lon": round(lon, 4),
            "alt_km": round(alt, 1),
            "speed_kms": round(math.sqrt(sum(v * v for v in vel)), 2),
        })
        if len(out) >= limit:
            break
    return {"count": len(out), "satellites": out}


# ---------------------------------------------------------------- launches

def get_launches(limit: int = 8) -> Dict[str, Any]:
    def _fetch():
        headers = dict(UA)
        if _env("LL2_API_TOKEN"):
            headers["Authorization"] = f"Token {_env('LL2_API_TOKEN')}"
        resp = requests.get("https://ll.thespacedevs.com/2.3.0/launches/upcoming/", params={"limit": limit, "mode": "detailed"}, headers=headers, timeout=20)
        resp.raise_for_status()
        return resp.json().get("results", [])

    try:
        rows = _cached("launches", 900, _fetch)
    except Exception as exc:
        return {"count": 0, "launches": [], "error": str(exc)[:120]}
    out = []
    for r in rows:
        pad = r.get("pad") or {}
        loc = pad.get("location") or {}
        out.append({
            "id": r.get("id", ""),
            "name": r.get("name", ""),
            "net": r.get("net", ""),
            "status": (r.get("status") or {}).get("name", ""),
            "provider": (r.get("launch_service_provider") or {}).get("name", ""),
            "pad": pad.get("name", ""),
            "location": loc.get("name", ""),
            "lat": _num(pad.get("latitude")),
            "lon": _num(pad.get("longitude")),
        })
    return {"count": len(out), "launches": out}


# ---------------------------------------------------------------- fires (needs FIRMS_MAP_KEY)

def get_fires(bbox: Optional[Tuple[float, float, float, float]] = None) -> Dict[str, Any]:
    key = _env("FIRMS_MAP_KEY")
    if not key:
        return {"count": 0, "fires": [], "error": "FIRMS_MAP_KEY not set"}
    area = "world" if not bbox else f"{bbox[1]},{bbox[0]},{bbox[3]},{bbox[2]}"

    def _fetch():
        rows: List[Dict[str, Any]] = []
        last_exc: Optional[Exception] = None
        for source in ("VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT", "VIIRS_SNPP_NRT"):
            try:
                resp = requests.get(f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{source}/{area}/1", headers=UA, timeout=40)
                resp.raise_for_status()
                rows.extend(csv.DictReader(io.StringIO(resp.text)))
            except Exception as exc:  # one satellite being down must not blank the layer
                last_exc = exc
        if not rows and last_exc:
            raise last_exc
        return rows

    try:
        rows = _cached(f"fires:{area}", 1800, _fetch)
    except Exception as exc:
        return {"count": 0, "fires": [], "error": str(exc)[:120]}
    out = [
        {"lat": float(r["latitude"]), "lon": float(r["longitude"]), "frp": _num(r.get("frp")), "date": r.get("acq_date", ""), "conf": r.get("confidence", "")}
        for r in rows
        if r.get("latitude") and r.get("longitude")
    ]
    out.sort(key=lambda f: f["frp"] or 0, reverse=True)
    return {"count": len(out), "fires": out[:600]}


# ---------------------------------------------------------------- OpenSky fallback (OAuth client credentials)

_opensky_token: Dict[str, Any] = {"value": "", "exp": 0.0}


def _opensky_headers() -> Dict[str, str]:
    cid, secret = _env("OPENSKY_CLIENT_ID"), _env("OPENSKY_CLIENT_SECRET")
    if not cid or not secret:
        return {}
    if _opensky_token["value"] and time.time() < _opensky_token["exp"] - 30:
        return {"Authorization": f"Bearer {_opensky_token['value']}"}
    resp = requests.post(
        "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token",
        data={"grant_type": "client_credentials", "client_id": cid, "client_secret": secret},
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json()
    _opensky_token.update(value=body["access_token"], exp=time.time() + float(body.get("expires_in", 300)))
    return {"Authorization": f"Bearer {_opensky_token['value']}"}


def _opensky_aircraft(lat: float, lon: float, radius_nm: float) -> Optional[Dict[str, Any]]:
    try:
        dlat = radius_nm * 1.852 / 111.0
        dlon = dlat / max(0.2, math.cos(math.radians(lat)))
        params = {"lamin": lat - dlat, "lamax": lat + dlat, "lomin": lon - dlon, "lomax": lon + dlon}
        resp = requests.get("https://opensky-network.org/api/states/all", params=params, headers={**UA, **_opensky_headers()}, timeout=20)
        resp.raise_for_status()
        states = resp.json().get("states") or []
    except Exception:
        return None
    out = []
    for s in states:
        if s[5] is None or s[6] is None or s[8]:
            continue
        alt_m = s[7] if s[7] is not None else s[13]
        out.append({
            "id": s[0],
            "callsign": (s[1] or "").strip(),
            "reg": "",
            "type": "",
            "lat": s[6],
            "lon": s[5],
            "alt_ft": round((alt_m or 0) * 3.28084),
            "speed_kt": round(s[9] * 1.94384) if s[9] is not None else None,
            "heading": s[10],
            "squawk": s[14] or "",
            "military": False,
        })
    return {"count": len(out[:400]), "aircraft": out[:400], "source": "opensky"}


# ---------------------------------------------------------------- ships (AISStream, needs AISSTREAM_API_KEY)

_ships: Dict[str, Dict[str, Any]] = {}
_SHIP_TTL = 15 * 60


async def _collect_ships(bbox: Tuple[float, float, float, float], seconds: float) -> None:
    import asyncio

    import websockets

    s, w, n, e = bbox
    sub = {
        "APIKey": _env("AISSTREAM_API_KEY"),
        "BoundingBoxes": [[[s, w], [n, e]]],
        "FilterMessageTypes": ["PositionReport"],
    }
    deadline = time.time() + seconds
    async with websockets.connect("wss://stream.aisstream.io/v0/stream", open_timeout=10) as ws:
        await ws.send(json.dumps(sub))
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.time()))
            except asyncio.TimeoutError:
                break
            msg = json.loads(raw)
            if "error" in msg:
                raise RuntimeError(str(msg["error"])[:100])
            meta, pos = msg.get("MetaData", {}), msg.get("Message", {}).get("PositionReport")
            if not pos or meta.get("latitude") is None:
                continue
            mmsi = str(meta.get("MMSI", ""))
            heading = pos.get("TrueHeading")
            _ships[mmsi] = {
                "id": mmsi,
                "name": (meta.get("ShipName") or "").strip() or mmsi,
                "lat": meta["latitude"],
                "lon": meta["longitude"],
                "speed_kt": pos.get("Sog"),
                "course": pos.get("Cog"),
                "heading": heading if heading is not None and heading < 360 else pos.get("Cog"),
                "status": pos.get("NavigationalStatus"),
                "seen": time.time(),
            }


def get_ships(bbox: Tuple[float, float, float, float], listen_seconds: float = 5.0) -> Dict[str, Any]:
    if not _env("AISSTREAM_API_KEY"):
        return {"count": 0, "ships": [], "error": "AISSTREAM_API_KEY not set"}
    import asyncio

    s, w, n, e = bbox
    if (n - s) > 40 or (e - w) > 60:  # keep the subscription regional
        return {"count": 0, "ships": [], "error": "zoom in to see ships"}
    error = None
    try:
        asyncio.run(_collect_ships(bbox, listen_seconds))
    except Exception as exc:
        error = str(exc)[:120] or exc.__class__.__name__
    now = time.time()
    for mmsi in [k for k, v in _ships.items() if now - v["seen"] > _SHIP_TTL]:
        _ships.pop(mmsi, None)
    out = [dict(v) for v in _ships.values() if _in_bbox(v["lat"], v["lon"], bbox)][:800]
    for v in out:
        v["age_s"] = int(now - v.pop("seen"))
    res: Dict[str, Any] = {"count": len(out), "ships": out}
    if error and not out:
        res["error"] = error
    return res


# ---------------------------------------------------------------- traffic (TomTom, needs TOMTOM_API_KEY)

def traffic_tile(z: int, x: int, y: int) -> bytes:
    key = _env("TOMTOM_API_KEY")
    if not key:
        raise LookupError("TOMTOM_API_KEY not set")
    n = 2 ** z
    if not (0 <= z <= 22 and 0 <= x < n and 0 <= y < n):
        raise ValueError("bad tile")

    def _fetch():
        resp = requests.get(f"https://api.tomtom.com/traffic/map/4/tile/flow/relative0/{z}/{x}/{y}.png", params={"key": key}, timeout=15)
        resp.raise_for_status()
        return resp.content

    return _cached(f"tile:{z}:{x}:{y}", 120, _fetch)


def get_traffic_flow(lat: float, lon: float) -> Dict[str, Any]:
    key = _env("TOMTOM_API_KEY")
    if not key:
        return {"error": "TOMTOM_API_KEY not set"}
    try:
        resp = requests.get(
            "https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/10/json",
            params={"key": key, "point": f"{lat:.5f},{lon:.5f}"},
            timeout=15,
        )
        resp.raise_for_status()
        seg = resp.json()["flowSegmentData"]
    except Exception as exc:
        return {"error": str(exc)[:120]}
    cur, free = seg.get("currentSpeed") or 0, seg.get("freeFlowSpeed") or 0
    ratio = cur / free if free else 1.0
    level = "flowing freely" if ratio >= 0.85 else "moderate" if ratio >= 0.6 else "heavy" if ratio >= 0.35 else "stop-and-go"
    return {
        "speed_kmh": cur,
        "free_flow_kmh": free,
        "delay_s": max(0, (seg.get("currentTravelTime") or 0) - (seg.get("freeFlowTravelTime") or 0)),
        "level": level,
        "closed": bool(seg.get("roadClosure")),
        "lat": lat,
        "lon": lon,
    }


_INCIDENT_TYPES = {0: "Unknown", 1: "Accident", 2: "Fog", 3: "Dangerous conditions", 4: "Rain", 5: "Ice", 6: "Jam", 7: "Lane closed", 8: "Road closed", 9: "Road works", 10: "Wind", 11: "Flooding", 14: "Broken-down vehicle"}


def get_traffic_incidents(bbox: Tuple[float, float, float, float]) -> Dict[str, Any]:
    key = _env("TOMTOM_API_KEY")
    if not key:
        return {"count": 0, "incidents": [], "error": "TOMTOM_API_KEY not set"}
    s, w, n, e = bbox
    if (n - s) > 1.2 or (e - w) > 1.6:  # TomTom caps the incident query area
        return {"count": 0, "incidents": [], "error": "zoom in to see incidents"}

    def _fetch():
        resp = requests.get(
            "https://api.tomtom.com/traffic/services/5/incidentDetails",
            params={
                "key": key,
                "bbox": f"{w},{s},{e},{n}",
                "fields": "{incidents{type,geometry{type,coordinates},properties{iconCategory,events{description},from,to,roadNumbers,delay}}}",
                "language": "en-GB",
            },
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json().get("incidents", [])

    try:
        rows = _cached(f"inc:{round(s, 2)}:{round(w, 2)}:{round(n, 2)}:{round(e, 2)}", 120, _fetch)
    except Exception as exc:
        return {"count": 0, "incidents": [], "error": str(exc)[:120]}
    out = []
    for r in rows:
        p, g = r.get("properties", {}), r.get("geometry", {})
        coords = g.get("coordinates") or []
        pt = coords[0] if g.get("type") == "LineString" and coords else coords
        if not pt or not isinstance(pt, list) or len(pt) < 2 or isinstance(pt[0], list):
            continue
        events = ", ".join(ev.get("description", "") for ev in p.get("events", []) if ev.get("description"))
        out.append({
            "type": _INCIDENT_TYPES.get(p.get("iconCategory"), "Incident"),
            "description": events,
            "road": ", ".join(p.get("roadNumbers") or []),
            "from": p.get("from") or "",
            "to": p.get("to") or "",
            "delay_s": p.get("delay"),
            "lat": pt[1],
            "lon": pt[0],
        })
    return {"count": len(out), "incidents": out[:300]}


# ---------------------------------------------------------------- status

def get_status() -> Dict[str, Any]:
    return {
        "layers": {
            "cameras": {"key": None, "ready": True},
            "aircraft": {"key": "OPENSKY_CLIENT_ID (fallback)", "ready": True, "fallback": bool(_env("OPENSKY_CLIENT_ID"))},
            "satellites": {"key": None, "ready": Satrec is not None},
            "quakes": {"key": None, "ready": True},
            "launches": {"key": "LL2_API_TOKEN (optional)", "ready": True},
            "fires": {"key": "FIRMS_MAP_KEY", "ready": bool(_env("FIRMS_MAP_KEY"))},
            "ships": {"key": "AISSTREAM_API_KEY", "ready": bool(_env("AISSTREAM_API_KEY"))},
            "traffic": {"key": "TOMTOM_API_KEY", "ready": bool(_env("TOMTOM_API_KEY"))},
        },
        "cameras_indexed": len(_cameras),
        "catalogs": _catalog_meta,
    }


# ---------------------------------------------------------------- chat intents

_PLACE_RE = re.compile(r"^.*\b(?:in|around|near|over|at|from|across|within|by|of)\s+(?:the\s+)?(.+?)\s*[?.!]*$", re.I)
_LEAD_NOISE = re.compile(r"^(?:some|any|all|the|a few|few|local|live|public|traffic|road)\s+", re.I)
_TRAILING_NOISE = re.compile(r"\s+(?:right now|now|today|please|for me|tonight|currently)$", re.I)

_CAM_RE = re.compile(r"\b(cameras?|cams?|cctv|webcams?)\b", re.I)
_QUAKE_RE = re.compile(r"\b(earthquakes?|quakes?|seismic|tremors?)\b", re.I)
_AIR_RE = re.compile(r"\b(planes?|aircraft|airplanes?|flights?|jets?|helicopters?|flying)\b", re.I)
_SAT_RE = re.compile(r"\b(satellites?|iss|space station|starlink|tiangong)\b", re.I)
_LAUNCH_RE = re.compile(r"\b(rocket launch(?:es)?|launch(?:es)? schedule|upcoming launch(?:es)?|next launch|spacex launch)\b", re.I)
_FIRE_RE = re.compile(r"\b(wild ?fires?|active fires?|fires? (?:near|in|around))\b", re.I)
_WORLD_RE = re.compile(r"\b(world (?:tab|map|view)|god'?s eye|open (?:the )?world|global map)\b", re.I)
_SHIP_RE = re.compile(r"\b(ships?|vessels?|boats?|tankers?|cargo ships?|cruise ships?|ais)\b", re.I)
_TRAFFIC_RE = re.compile(r"\b(traffic|congestion|commute)\b", re.I)
_INCIDENT_RE = re.compile(r"\b(accidents?|crashes|road closures?|road works?|incidents?|roadwork)\b", re.I)
_VERB_RE = re.compile(r"\b(pull up|show|open|find|bring up|look at|check|list|display|see|where|what|any|are there|who|how many|how is|how's|how bad|is there|track|get)\b", re.I)


def _extract_place(message: str) -> str:
    m = _PLACE_RE.search(message.strip())
    if not m:
        return ""
    place = _TRAILING_NOISE.sub("", m.group(1).strip())
    place = _LEAD_NOISE.sub("", place).strip(" ,.")
    if place.lower() in {"me", "here", "my location", "my area", "home", "the world", "the sky", "world", "the area", "us"}:
        return ""
    return place


def _default_center() -> Tuple[float, float]:
    try:
        return float(_env("FUTURE_LOCATION_LAT") or 44.48), float(_env("FUTURE_LOCATION_LNG") or -93.43)
    except ValueError:
        return 44.48, -93.43


def _resolve_center(message: str) -> Tuple[float, float, str, Optional[List[float]]]:
    place = _extract_place(message)
    if place:
        geo = geocode(place)
        if geo:
            return geo["lat"], geo["lon"], place, geo.get("bbox")
    lat, lon = _default_center()
    return lat, lon, "", None


def handle_chat(message: str) -> Optional[Dict[str, Any]]:
    """Return {reply, action} when the message is a world-data request, otherwise None."""
    text = (message or "").strip()
    low = text.lower()
    if not text or len(text) > 300:
        return None

    if _WORLD_RE.search(low) and not _CAM_RE.search(low):
        return {"reply": "Opening the World tab.", "action": {"type": "world", "layers": ["cameras", "quakes"]}}

    if _CAM_RE.search(low) and _VERB_RE.search(low):
        place = _extract_place(text)
        res = search_cameras(place=place) if place else search_cameras(lat=_default_center()[0], lon=_default_center()[1], radius_km=60)
        if not res["cameras"]:
            return {"reply": f"I couldn't find any public cameras{' around ' + place if place else ''}.", "action": None}
        where = place or "your area"
        reply = f"Found {res['count']} public cameras around {where}. I've opened the World tab with the closest {len(res['cameras'])}; tap one to view it live."
        return {"reply": reply, "action": {"type": "world", "layers": ["cameras"], "center": res["center"], "bbox": res["bbox"], "query": where, "cameras": res["cameras"]}}

    if _QUAKE_RE.search(low):
        big = bool(re.search(r"\b(big|major|strong|largest|significant|biggest)\b", low))
        week = bool(re.search(r"\b(week|7 days)\b", low))
        feed = ("4.5_week" if week else "4.5_day") if big else ("2.5_week" if week else "2.5_day")
        res = get_quakes(feed)
        quakes = res["quakes"]
        place = _extract_place(text)
        center = None
        if place:
            geo = geocode(place)
            if geo:
                center = (geo["lat"], geo["lon"])
                quakes = [q for q in quakes if haversine_km(center[0], center[1], q["lat"], q["lon"]) <= 1000]
        if res.get("error"):
            return {"reply": "The earthquake feed is unavailable right now.", "action": None}
        top = sorted(quakes, key=lambda q: q["mag"], reverse=True)[:5]
        if not top:
            reply = f"No quakes of that size{' near ' + place if place else ''} in the {'past week' if week else 'last day'}."
        else:
            lines = "; ".join(f"M{q['mag']} {q['place']}" for q in top)
            reply = f"{len(quakes)} quakes{' near ' + place if place else ''} in the {'past week' if week else 'last 24h'}. Largest: {lines}."
        return {"reply": reply + " Plotted on the World tab.", "action": {"type": "world", "layers": ["quakes"], "center": list(center) if center else None, "feed": feed}}

    if _SAT_RE.search(low) and _VERB_RE.search(low):
        iss = bool(re.search(r"\b(iss|space station)\b", low))
        group = "stations" if iss or "station" in low else ("starlink" if "starlink" in low else "visual")
        res = get_satellites(group, name="ISS" if iss else "", limit=300)
        if res.get("error") or not res["satellites"]:
            return {"reply": "Satellite data is unavailable right now.", "action": None}
        if iss:
            s = res["satellites"][0]
            reply = f"The ISS is at {s['lat']:.2f}, {s['lon']:.2f}, {s['alt_km']:.0f} km up, moving {s['speed_kms']} km/s."
            return {"reply": reply, "action": {"type": "world", "layers": ["satellites"], "center": [s["lat"], s["lon"]], "sat_group": group, "zoom": 3}}
        return {"reply": f"Tracking {res['count']} satellites live on the World tab.", "action": {"type": "world", "layers": ["satellites"], "sat_group": group, "zoom": 2}}

    if _LAUNCH_RE.search(low):
        res = get_launches(6)
        if res.get("error") or not res["launches"]:
            return {"reply": "Launch schedule is unavailable right now.", "action": None}
        lines = "; ".join(f"{l['name']} ({l['net'][:16].replace('T', ' ')} UTC, {l['location']})" for l in res["launches"][:4])
        return {"reply": f"Upcoming launches: {lines}.", "action": {"type": "world", "layers": ["launches"]}}

    if _FIRE_RE.search(low):
        lat, lon, place, bb = _resolve_center(text)
        res = get_fires(tuple(bb) if bb else (lat - 3, lon - 4, lat + 3, lon + 4))
        if res.get("error"):
            return {"reply": f"Fire data needs a free NASA FIRMS key (FIRMS_MAP_KEY). {res['error']}.", "action": None}
        return {"reply": f"{res['count']} active fire detections in the last 24h{' near ' + place if place else ''}. Plotted on the World tab.", "action": {"type": "world", "layers": ["fires"], "center": [lat, lon]}}

    if _SHIP_RE.search(low) and _VERB_RE.search(low):
        lat, lon, place, bb = _resolve_center(text)
        box = tuple(bb) if bb and (bb[2] - bb[0]) < 20 and (bb[3] - bb[1]) < 30 else (lat - 2, lon - 3, lat + 2, lon + 3)
        res = get_ships(box)
        if res.get("error") and not res["ships"]:
            return {"reply": f"Ship tracking isn't available right now: {res['error']}.", "action": None}
        moving = [s for s in res["ships"] if (s.get("speed_kt") or 0) > 1]
        named = [s for s in res["ships"] if s["name"] != s["id"]][:5]
        names = ", ".join(s["name"] for s in named)
        reply = f"{res['count']} vessels{' near ' + place if place else ' in range'}, {len(moving)} underway." + (f" Including {names}." if names else "")
        return {"reply": reply + " Plotted on the World tab.", "action": {"type": "world", "layers": ["ships"], "center": [lat, lon], "bbox": list(box), "zoom": 8}}

    if _INCIDENT_RE.search(low) and re.search(r"\b(road|traffic|highway|freeway|interstate|drive|driving|near|in|around)\b", low) and _VERB_RE.search(low):
        lat, lon, place, _ = _resolve_center(text)
        res = get_traffic_incidents((lat - 0.25, lon - 0.35, lat + 0.25, lon + 0.35))
        if res.get("error"):
            return {"reply": f"Traffic incidents aren't available right now: {res['error']}.", "action": None}
        top = "; ".join(f"{i['type']} {i['road'] or i['from']}".strip() for i in res["incidents"][:5])
        reply = f"{res['count']} traffic incidents{' near ' + place if place else ' near you'}." + (f" {top}." if top else "")
        return {"reply": reply + " Shown on the World tab.", "action": {"type": "world", "layers": ["incidents", "traffic"], "center": [lat, lon], "zoom": 11}}

    if _TRAFFIC_RE.search(low) and _VERB_RE.search(low):
        lat, lon, place, _ = _resolve_center(text)
        flow = get_traffic_flow(lat, lon)
        if flow.get("error"):
            return {"reply": f"Traffic data isn't available right now: {flow['error']}.", "action": None}
        reply = f"Traffic{' near ' + place if place else ' near you'} is {flow['level']}: {flow['speed_kmh']} km/h against {flow['free_flow_kmh']} km/h free-flow" + (f", about {flow['delay_s'] // 60} min of delay" if flow["delay_s"] >= 60 else "") + "."
        return {"reply": reply + " Congestion is on the World tab.", "action": {"type": "world", "layers": ["traffic", "incidents"], "center": [lat, lon], "zoom": 11}}

    if _AIR_RE.search(low) and (_VERB_RE.search(low) or re.search(r"\b(overhead|above|in the sky|flying)\b", low)):
        mil = bool(re.search(r"\b(military|mil|fighter|tanker)\b", low))
        lat, lon, place, _ = _resolve_center(text)
        res = get_aircraft(lat, lon, 150, military=mil)
        if res.get("error"):
            return {"reply": "The aircraft feed is unavailable right now.", "action": None}
        sample = [a for a in res["aircraft"] if a["callsign"]][:6]
        names = ", ".join(f"{a['callsign']} ({int(a['alt_ft'] or 0):,} ft)" for a in sample)
        label = place or "you"
        reply = f"{res['count']} {'military ' if mil else ''}aircraft within 150 nm of {label}." + (f" Including {names}." if names else "")
        return {"reply": reply + " Live on the World tab.", "action": {"type": "world", "layers": ["aircraft"], "center": [lat, lon], "military": mil}}

    return None
