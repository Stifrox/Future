import json

from tools import worldview as wv


def _cat(**over):
    cat = {
        "id": "t1",
        "provider": "Test",
        "country": "US",
        "countryName": "Testland",
        "arrayPath": "features",
        "fields": {"id": "properties.id", "name": "properties.name", "lat": "geometry.coordinates.1", "lon": "geometry.coordinates.0", "imageUrl": "properties.img"},
    }
    cat.update(over)
    return cat


def test_build_camera_from_geojson_row():
    row = {"properties": {"id": 7, "name": "I-94  at 5th", "img": "https://x.test/a.jpg"}, "geometry": {"coordinates": [-93.2, 44.9]}}
    cam = wv._build_camera(_cat(), row)
    assert cam["id"] == "t1:7"
    assert cam["name"] == "I-94 at 5th"
    assert cam["url"] == "https://x.test/a.jpg"


def test_build_camera_template_and_prefix_stripping():
    cat = _cat(idStripPrefix="Cam_", imageUrlTemplate="https://x.test/{id}.jpg")
    cat["fields"]["imageUrl"] = None
    row = {"properties": {"id": "Cam_9", "name": "n"}, "geometry": {"coordinates": [1, 2]}}
    assert wv._build_camera(cat, row)["url"] == "https://x.test/9.jpg"


def test_build_camera_rejects_bad_coordinates_and_non_http():
    bad_geo = {"properties": {"id": 1, "name": "n", "img": "https://x.test/a.jpg"}, "geometry": {"coordinates": [0, 0]}}
    assert wv._build_camera(_cat(), bad_geo) is None
    bad_url = {"properties": {"id": 1, "name": "n", "img": "javascript:alert(1)"}, "geometry": {"coordinates": [1, 2]}}
    assert wv._build_camera(_cat(), bad_url) is None


def test_search_cameras_by_bbox_sorts_by_distance(monkeypatch):
    cams = [
        {"id": "a:1", "name": "far", "lat": 45.5, "lon": -93.0, "heading": "", "provider": "P", "region": "R", "country": "US", "url": "https://x/1"},
        {"id": "a:2", "name": "near", "lat": 44.98, "lon": -93.27, "heading": "", "provider": "P", "region": "R", "country": "US", "url": "https://x/2"},
        {"id": "a:3", "name": "outside", "lat": 10.0, "lon": 10.0, "heading": "", "provider": "P", "region": "R", "country": "US", "url": "https://x/3"},
    ]
    monkeypatch.setattr(wv, "load_cameras", lambda force=False: cams)
    res = wv.search_cameras(lat=44.98, lon=-93.27, bbox=(43, -97, 49, -89))
    assert [c["id"] for c in res["cameras"]] == ["a:2", "a:1"]
    assert "url" not in res["cameras"][0]


def test_fetch_frame_unknown_camera(monkeypatch):
    monkeypatch.setattr(wv, "load_cameras", lambda force=False: [])
    wv._cameras_by_id.clear()
    try:
        wv.fetch_camera_frame("nope:1")
    except LookupError:
        return
    raise AssertionError("expected LookupError")


def test_place_extraction_uses_last_preposition():
    assert wv._extract_place("pull up some of the cameras around minnesota") == "minnesota"
    assert wv._extract_place("show me cameras near Saint Paul right now") == "Saint Paul"
    assert wv._extract_place("show me cameras") == ""


def test_handle_chat_ignores_unrelated_messages():
    assert wv.handle_chat("how do I make a plane in fusion 360") is None
    assert wv.handle_chat("what is the capital of France") is None


def test_handle_chat_cameras_returns_world_action(monkeypatch):
    fake = {"query": "Minnesota", "center": [45, -93], "bbox": None, "count": 2, "cameras": [{"id": "a:1", "name": "x", "lat": 1, "lon": 2}]}
    monkeypatch.setattr(wv, "search_cameras", lambda **kw: fake)
    out = wv.handle_chat("pull up some of the cameras around minnesota")
    assert out["action"]["type"] == "world"
    assert out["action"]["cameras"] == fake["cameras"]
    json.dumps(out)


def test_quake_parsing(monkeypatch):
    feature = {"id": "u1", "properties": {"mag": 5.1, "place": "Somewhere", "time": 1_700_000_000_000, "url": "u", "tsunami": 0}, "geometry": {"coordinates": [10, 20, 30]}}
    monkeypatch.setattr(wv, "_cached", lambda key, ttl, fn: [feature])
    res = wv.get_quakes("2.5_day")
    assert res["quakes"][0]["lat"] == 20 and res["quakes"][0]["mag"] == 5.1


def test_fires_need_key(monkeypatch):
    monkeypatch.delenv("FIRMS_MAP_KEY", raising=False)
    assert wv.get_fires()["error"]


def test_traffic_flow_levels(monkeypatch):
    monkeypatch.setenv("TOMTOM_API_KEY", "k")

    class R:
        ok = True

        def raise_for_status(self):
            pass

        def json(self):
            return {"flowSegmentData": {"currentSpeed": 20, "freeFlowSpeed": 32, "currentTravelTime": 93, "freeFlowTravelTime": 58, "roadClosure": False}}

    monkeypatch.setattr(wv.requests, "get", lambda *a, **k: R())
    flow = wv.get_traffic_flow(44.9, -93.2)
    assert flow["level"] == "moderate" and flow["delay_s"] == 35


def test_traffic_requires_key(monkeypatch):
    monkeypatch.delenv("TOMTOM_API_KEY", raising=False)
    assert wv.get_traffic_flow(1, 1)["error"]
    assert wv.get_traffic_incidents((44, -94, 45, -93))["error"]


def test_incident_query_rejects_huge_area(monkeypatch):
    monkeypatch.setenv("TOMTOM_API_KEY", "k")
    assert "zoom in" in wv.get_traffic_incidents((0, 0, 40, 40))["error"]


def test_incident_parsing(monkeypatch):
    monkeypatch.setenv("TOMTOM_API_KEY", "k")
    row = {"properties": {"iconCategory": 8, "from": "A", "to": "B", "roadNumbers": ["I-94"], "events": [{"description": "Closed"}], "delay": 120}, "geometry": {"type": "LineString", "coordinates": [[-93.5, 44.9], [-93.4, 44.9]]}}
    monkeypatch.setattr(wv, "_cached", lambda key, ttl, fn: [row])
    res = wv.get_traffic_incidents((44.8, -93.6, 45.0, -93.3))
    assert res["incidents"][0]["type"] == "Road closed" and res["incidents"][0]["lat"] == 44.9


def test_traffic_tile_validates_coordinates(monkeypatch):
    monkeypatch.setenv("TOMTOM_API_KEY", "k")
    try:
        wv.traffic_tile(2, 9, 0)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_ships_need_key_and_regional_bbox(monkeypatch):
    monkeypatch.delenv("AISSTREAM_API_KEY", raising=False)
    assert wv.get_ships((0, 0, 1, 1))["error"]
    monkeypatch.setenv("AISSTREAM_API_KEY", "k")
    assert "zoom in" in wv.get_ships((-60, -100, 60, 100))["error"]


def test_ships_returns_cached_in_bbox(monkeypatch):
    monkeypatch.setenv("AISSTREAM_API_KEY", "k")
    wv._ships.clear()
    wv._ships["1"] = {"id": "1", "name": "A", "lat": 10, "lon": 10, "speed_kt": 5, "course": 0, "heading": 0, "status": 0, "seen": wv.time.time()}
    wv._ships["2"] = {"id": "2", "name": "B", "lat": 50, "lon": 50, "speed_kt": 5, "course": 0, "heading": 0, "status": 0, "seen": wv.time.time()}

    async def noop(bbox, seconds):
        return None

    monkeypatch.setattr(wv, "_collect_ships", noop)
    res = wv.get_ships((5, 5, 15, 15))
    assert [s["id"] for s in res["ships"]] == ["1"]


def test_opensky_fallback_parsing(monkeypatch):
    state = ["abc123", "UAL1  ", "US", 0, 0, -93.2, 44.9, 1000.0, False, 200.0, 90.0, 0, None, 1100.0, "1200"]

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"states": [state]}

    monkeypatch.setattr(wv, "_opensky_headers", lambda: {})
    monkeypatch.setattr(wv.requests, "get", lambda *a, **k: R())
    res = wv._opensky_aircraft(44.9, -93.2, 100)
    assert res["aircraft"][0]["callsign"] == "UAL1" and res["aircraft"][0]["alt_ft"] == 3281


def test_handle_chat_traffic_and_ships(monkeypatch):
    monkeypatch.setattr(wv, "geocode", lambda q: {"name": q, "lat": 44.9, "lon": -93.2, "bbox": None})
    monkeypatch.setattr(wv, "get_traffic_flow", lambda lat, lon: {"level": "heavy", "speed_kmh": 20, "free_flow_kmh": 60, "delay_s": 300})
    out = wv.handle_chat("how is traffic in minneapolis right now")
    assert "heavy" in out["reply"] and "traffic" in out["action"]["layers"]
    monkeypatch.setattr(wv, "get_ships", lambda bbox, listen_seconds=5.0: {"count": 1, "ships": [{"id": "9", "name": "ATLAS", "speed_kt": 8}]})
    out = wv.handle_chat("show me ships near long beach")
    assert "ATLAS" in out["reply"] and out["action"]["layers"] == ["ships"]
