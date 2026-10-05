"""Tools for the Columbia Commuter agent, and the JSON that describes them to the model.

Every data source here is free and needs no API key:
- MTA GTFS-realtime feeds (live subway arrivals and service alerts)
- Open-Meteo (weather)
- OpenStreetMap Nominatim (geocoding places that aren't in our campus table)

Every tool returns a JSON string. On failure it returns {"error": ..., "hint": ...}
so the model can tell the user what went wrong or retry with better arguments.
"""

import json
import math
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from google.transit import gtfs_realtime_pb2

NYC = ZoneInfo("America/New_York")

# --- Data sources (all free, no key) ---

MTA_123_FEED = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs"
MTA_ALERTS_FEED = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/camsys%2Fsubway-alerts.json"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim's usage policy requires an identifying User-Agent.
HEADERS = {"User-Agent": "columbia-commuter-agent/1.0 (class project)"}

# --- Static reference data ---

# Broadway-7th Av line (1/2/3) stations in Manhattan and the Bronx, GTFS stop_id -> name.
# The 2/3 only stop at the express stations (96, 72, Times Sq, 34, 14, Chambers).
STATIONS = {
    "101": "Van Cortlandt Park-242 St", "103": "238 St", "104": "231 St",
    "106": "Marble Hill-225 St", "107": "215 St", "108": "207 St", "109": "Dyckman St",
    "110": "191 St", "111": "181 St", "112": "168 St-Washington Hts", "113": "157 St",
    "114": "145 St", "115": "137 St-City College", "116": "125 St",
    "117": "116 St-Columbia University", "118": "Cathedral Pkwy (110 St)", "119": "103 St",
    "120": "96 St", "121": "86 St", "122": "79 St", "123": "72 St",
    "124": "66 St-Lincoln Center", "125": "59 St-Columbus Circle", "126": "50 St",
    "127": "Times Sq-42 St", "128": "34 St-Penn Station", "129": "28 St", "130": "23 St",
    "131": "18 St", "132": "14 St", "133": "Christopher St-Sheridan Sq", "134": "Houston St",
    "135": "Canal St", "136": "Franklin St", "137": "Chambers St", "138": "WTC Cortlandt",
    "139": "Rector St", "142": "South Ferry",
}

# Campus landmarks (lat, lon), so walking estimates work without a geocoder.
# Keys are lowercase aliases; several aliases can point at the same spot.
_LERNER = (40.8069, -73.9636)
_JOHN_JAY = (40.8061, -73.9618)
PLACES = {
    "116 st station": (40.8078, -73.9640),
    "116 st-columbia university": (40.8078, -73.9640),
    "cathedral pkwy (110 st)": (40.8036, -73.9667),
    "110 st station": (40.8036, -73.9667),
    "125 st station": (40.8156, -73.9585),
    "college walk": (40.8075, -73.9626),
    "columbia": (40.8075, -73.9626),
    "low library": (40.8080, -73.9621),
    "butler library": (40.8064, -73.9630),
    "butler": (40.8064, -73.9630),
    "lerner hall": _LERNER,
    "lerner": _LERNER,
    "ferris booth commons": _LERNER,
    "john jay dining hall": _JOHN_JAY,
    "john jay": _JOHN_JAY,
    "jj's place": _JOHN_JAY,
    "hamilton hall": (40.8069, -73.9612),
    "northwest corner building": (40.8099, -73.9617),
    "pupin hall": (40.8099, -73.9612),
    "mudd": (40.8094, -73.9600),
    "uris hall": (40.8090, -73.9612),
    "dodge fitness center": (40.8093, -73.9628),
    "international affairs building": (40.8078, -73.9597),
    "sipa": (40.8078, -73.9597),
    "teachers college": (40.8104, -73.9601),
    "barnard": (40.8095, -73.9645),
    "milstein center": (40.8098, -73.9638),
    "hewitt dining hall": (40.8095, -73.9650),
    "manhattanville": (40.8165, -73.9575),
    "business school": (40.8174, -73.9568),
    "st john the divine": (40.8038, -73.9618),
    "hungarian pastry shop": (40.8035, -73.9633),
    "koronet pizza": (40.8034, -73.9651),
    "morningside park": (40.8065, -73.9580),
    "riverside park": (40.8095, -73.9688),
}

WALK_M_PER_MIN = 80  # about 3 mph
JOG_M_PER_MIN = 140  # a hurried jog with a backpack
GRID_FACTOR = 1.25   # streets aren't straight lines; scales crow-flies distance
STATION_BUFFER_MIN = 1.5  # stairs + OMNY tap + getting to the platform


# --- Helpers ---


def _error(message: str, hint: str = "") -> str:
    """The model can't see exceptions, so errors come back as data it can act on."""
    return json.dumps({"error": message, "hint": hint} if hint else {"error": message})


def _now() -> datetime:
    return datetime.now(NYC)


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts, NYC).strftime("%-I:%M %p")


def _find_station(name: str) -> str | None:
    """Match a user-typed station name ('116', 'columbia', 'times square') to a stop_id."""
    q = name.lower().replace("parkway", "pkwy").replace("street", "st").replace("square", "sq").replace("th ", " ").strip()
    q = q.removesuffix("th").removesuffix(" station").strip()
    if not q:
        return None
    # Exact or prefix matches first ("14 st" must not match "145 St"), then substrings.
    for stop_id, station in STATIONS.items():
        s = station.lower()
        if q == s or s.startswith(q + " ") or s.startswith(q + "-"):
            return stop_id
    for stop_id, station in STATIONS.items():
        if q in station.lower():
            return stop_id
    return None


def _resolve_place(place: str) -> tuple[float, float, str] | None:
    """Campus table first, then OpenStreetMap, restricted to the NYC area."""
    key = place.lower().strip()
    if key in PLACES:
        lat, lon = PLACES[key]
        return lat, lon, place
    for alias, (lat, lon) in PLACES.items():
        if key in alias or alias in key:
            return lat, lon, alias
    r = requests.get(
        NOMINATIM_URL,
        params={
            "q": place, "format": "json", "limit": 1,
            "viewbox": "-74.26,40.92,-73.70,40.49", "bounded": 1,  # NYC bounding box
        },
        headers=HEADERS, timeout=10,
    )
    r.raise_for_status()
    hits = r.json()
    if not hits:
        return None
    return float(hits[0]["lat"]), float(hits[0]["lon"]), hits[0]["display_name"].split(",")[0]


def _walk_minutes(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float]:
    """Return (meters, minutes) for walking between two lat/lon points."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    meters = 2 * 6_371_000 * math.asin(math.sqrt(h)) * GRID_FACTOR
    return meters, meters / WALK_M_PER_MIN


_feed_cache: dict[str, tuple[float, object]] = {}


def _fetch_trip_feed() -> gtfs_realtime_pb2.FeedMessage:
    """The 1/2/3 feed, cached for 20s so one chat turn doesn't hammer the MTA."""
    cached = _feed_cache.get("123")
    if cached and time.time() - cached[0] < 20:
        return cached[1]
    r = requests.get(MTA_123_FEED, headers=HEADERS, timeout=10)
    r.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(r.content)
    _feed_cache["123"] = (time.time(), feed)
    return feed


def _normalize_direction(direction: str) -> str | None:
    d = direction.lower().strip()
    if d in ("uptown", "north", "northbound", "n", "bronx"):
        return "N"
    if d in ("downtown", "south", "southbound", "s", "midtown"):
        return "S"
    if d in ("", "both", "any"):
        return ""
    return None


def _arrivals(stop_id: str, direction: str, route: str = "") -> list[dict]:
    """Upcoming arrivals at a stop, soonest first."""
    now = time.time()
    wanted = {stop_id + d for d in (direction or "NS")}
    out = []
    for entity in _fetch_trip_feed().entity:
        if not entity.HasField("trip_update"):
            continue
        trip = entity.trip_update.trip
        if route and trip.route_id != route:
            continue
        for stu in entity.trip_update.stop_time_update:
            if stu.stop_id not in wanted:
                continue
            ts = stu.arrival.time or stu.departure.time
            if ts and ts >= now - 30:
                out.append({
                    "route": trip.route_id,
                    "direction": "uptown" if stu.stop_id.endswith("N") else "downtown",
                    "arrives_at": _clock(ts),
                    "minutes_away": round((ts - now) / 60, 1),
                    "_ts": ts,
                })
    return sorted(out, key=lambda a: a["_ts"])


# --- Tools ---


def get_next_trains(station: str = "116 St-Columbia University", direction: str = "both",
                    route: str = "", limit: int = 6) -> str:
    """Live arrivals on the 1/2/3 at a Broadway-7th Av line station."""
    stop_id = _find_station(station)
    if not stop_id:
        return _error(
            f"'{station}' isn't a 1/2/3 station I know.",
            "Use a Broadway-7th Av line station such as '116 St-Columbia University', "
            "'Cathedral Pkwy (110 St)', '96 St', 'Times Sq-42 St'. Other lines aren't supported.",
        )
    d = _normalize_direction(direction)
    if d is None:
        return _error(f"Unknown direction '{direction}'.", "Use 'uptown', 'downtown' or 'both'.")
    if route and route not in ("1", "2", "3"):
        return _error(f"Route '{route}' isn't on this line.", "Use '1', '2', '3' or leave route empty.")
    try:
        arrivals = _arrivals(stop_id, d, route)
    except Exception as e:  # network error or a garbled protobuf
        return _error(f"MTA real-time feed unavailable: {e}",
                      "Tell the user live times are unavailable right now and suggest checking the MTA app.")
    for a in arrivals:
        a.pop("_ts")
    result = {
        "station": STATIONS[stop_id],
        "now": _now().strftime("%-I:%M %p"),
        "arrivals": arrivals[: max(1, min(limit, 12))],
    }
    if not arrivals:
        result["note"] = ("No trains are scheduled in the live feed. Service may be suspended "
                          "or rerouted here; check get_subway_alerts.")
    return json.dumps(result)


def get_subway_alerts(route: str = "1") -> str:
    """Active MTA delays, suspensions and planned work for one subway route."""
    route = route.upper().strip().removesuffix(" TRAIN")
    try:
        r = requests.get(MTA_ALERTS_FEED, headers=HEADERS, timeout=10)
        r.raise_for_status()
        entities = r.json().get("entity", [])
    except (requests.RequestException, ValueError) as e:
        return _error(f"MTA alerts feed unavailable: {e}", "Tell the user and suggest the MTA app or mta.info.")

    now = time.time()
    alerts = []
    for ent in entities:
        alert = ent.get("alert", {})
        if not any(ie.get("route_id") == route for ie in alert.get("informed_entity", [])):
            continue
        periods = alert.get("active_period", [])
        active = not periods or any(p.get("start", 0) <= now <= (p.get("end") or now + 1) for p in periods)
        if not active:
            continue
        text = next(
            (t["text"] for t in alert.get("header_text", {}).get("translation", []) if t.get("language") == "en"),
            "",
        )
        kind = alert.get("transit_realtime.mercury_alert", {}).get("alert_type", "Alert")
        alerts.append({"type": kind, "summary": text[:400]})

    # Real-time problems first, planned work after.
    alerts.sort(key=lambda a: a["type"] in ("Planned Work", "Special Schedule", "Station Notice"))
    return json.dumps({
        "route": route,
        "checked_at": _now().strftime("%-I:%M %p"),
        "active_alerts": alerts[:8],
        "status": "Good service - no active alerts" if not alerts else f"{len(alerts)} active alert(s)",
    })


def estimate_walk(origin: str, destination: str) -> str:
    """Walking distance and time between two places around Columbia / NYC."""
    try:
        a = _resolve_place(origin)
        b = _resolve_place(destination)
    except requests.RequestException as e:
        return _error(f"Geocoding service failed: {e}", "Retry with a campus building name or a street corner.")
    missing = [p for p, hit in ((origin, a), (destination, b)) if hit is None]
    if missing:
        return _error(
            f"Couldn't find {missing} in NYC.",
            "Try a campus building (Butler, Lerner, Mudd, SIPA, Barnard...) or an address like "
            "'Broadway and 110th St, New York'.",
        )
    meters, minutes = _walk_minutes(a[:2], b[:2])
    return json.dumps({
        "from": a[2], "to": b[2],
        "distance_miles": round(meters / 1609, 2),
        "walk_minutes": math.ceil(minutes),
        "jog_minutes": math.ceil(meters / JOG_M_PER_MIN),
        "note": "Estimate based on straight-line distance adjusted for the street grid.",
    })


def catch_the_train(start: str, direction: str, station: str = "116 St-Columbia University",
                    route: str = "") -> str:
    """For each upcoming train: can you walk to it, do you need to run, or is it gone?"""
    stop_id = _find_station(station)
    if not stop_id:
        return _error(f"'{station}' isn't a 1/2/3 station I know.",
                      "Use e.g. '116 St-Columbia University' or 'Cathedral Pkwy (110 St)'.")
    d = _normalize_direction(direction)
    if not d:
        return _error(f"Need a direction, got '{direction}'.",
                      "Ask the user if they're going 'uptown' or 'downtown', then retry.")
    try:
        origin = _resolve_place(start)
    except requests.RequestException as e:
        return _error(f"Geocoding failed: {e}", "Retry with a campus building name.")
    if origin is None:
        return _error(f"Couldn't find '{start}'.", "Ask the user which building or corner they're at.")
    station_coords = PLACES.get(STATIONS[stop_id].lower())
    if station_coords is None:
        try:
            hit = _resolve_place(STATIONS[stop_id] + " subway station, Manhattan")
        except requests.RequestException:
            hit = None
        if hit is None:
            return _error("Can only plan walks to stations near campus.",
                          "Use 116 St, Cathedral Pkwy (110 St) or 125 St.")
        station_coords = hit[:2]

    meters, walk_min = _walk_minutes(origin[:2], station_coords)
    jog_min = meters / JOG_M_PER_MIN
    try:
        arrivals = _arrivals(stop_id, d, route)[:5]
    except Exception as e:
        return _error(f"MTA real-time feed unavailable: {e}", "Tell the user live times are unavailable.")

    now = time.time()
    plan = []
    for a in arrivals:
        mins = (a["_ts"] - now) / 60
        slack = mins - walk_min - STATION_BUFFER_MIN
        if slack >= 2:
            verdict, leave = "easy walk", a["_ts"] - (walk_min + STATION_BUFFER_MIN + 1) * 60
        elif slack >= 0:
            verdict, leave = "leave right now", now
        elif mins - jog_min - STATION_BUFFER_MIN >= 0:
            verdict, leave = "only if you run", now
        else:
            verdict, leave = "you'll miss it", None
        plan.append({
            "route": a["route"], "arrives_at": a["arrives_at"], "minutes_away": a["minutes_away"],
            "verdict": verdict, "leave_by": _clock(leave) if leave else None,
        })
    result = {
        "from": origin[2], "station": STATIONS[stop_id],
        "direction": "uptown" if d == "N" else "downtown",
        "walk_to_station_minutes": math.ceil(walk_min),
        "now": _now().strftime("%-I:%M %p"),
        "trains": plan,
    }
    if not plan:
        result["note"] = "No upcoming trains in the live feed; check get_subway_alerts for suspensions."
    return json.dumps(result)


def get_weather(place: str = "Columbia") -> str:
    """Current conditions plus rain chance for the next few hours."""
    try:
        hit = _resolve_place(place)
        if hit is None:
            return _error(f"Couldn't find '{place}' in NYC.", "Use 'Columbia' or an NYC neighborhood.")
        lat, lon, name = hit
        data = requests.get(
            FORECAST_URL,
            params={
                "latitude": lat, "longitude": lon,
                "current": "temperature_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m",
                "hourly": "precipitation_probability",
                "forecast_hours": 4,
                "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
                "timezone": "America/New_York",
            },
            timeout=10,
        ).json()
        current = data["current"]
    except (requests.RequestException, KeyError, ValueError) as e:
        return _error(f"Weather service failed: {e}", "Tell the user weather is unavailable right now.")
    rain = data.get("hourly", {}).get("precipitation_probability", [])
    return json.dumps({
        "place": name,
        "temp_f": current["temperature_2m"],
        "feels_like_f": current["apparent_temperature"],
        "precipitation_now_in": current["precipitation"],
        "wind_mph": current["wind_speed_10m"],
        "max_rain_chance_next_4h_pct": max(rain) if rain else None,
    })


# --- Cafe / food spots for coffee_before_train ---
# HAND-CURATED. Hours, prices, ratings and coordinates are approximate and written from
# general knowledge, not a live source. Verify them on Google Maps and edit freely.
# To add a spot, copy a dict. hours = [(days, open, close)], days 0=Mon..6=Sun; a close at or
# before the open time means it runs past midnight. order_min = typical wait to get served.
_ALL = (0, 1, 2, 3, 4, 5, 6)
FOOD_SPOTS = [
    {"name": "Hungarian Pastry Shop", "coords": PLACES["hungarian pastry shop"], "kinds": ("coffee", "food"),
     "price": 1, "vibe": "cozy, cash-friendly, study-friendly", "rating": 4.5, "order_min": 4,
     "hours": [(_ALL, "8:00", "23:30")]},
    {"name": "Koronet Pizza", "coords": PLACES["koronet pizza"], "kinds": ("food",),
     "price": 1, "vibe": "grab-and-go, huge slices", "rating": 4.0, "order_min": 5,
     "hours": [(_ALL, "10:00", "01:00")]},
    {"name": "Tom's Restaurant", "coords": (40.8056, -73.9655), "kinds": ("food", "coffee"),
     "price": 2, "vibe": "sit-down diner", "rating": 3.5, "order_min": 20,
     "hours": [(_ALL, "6:00", "01:00")]},
    {"name": "Community Food & Juice", "coords": (40.8065, -73.9649), "kinds": ("food", "coffee"),
     "price": 2, "vibe": "sit-down brunch spot", "rating": 4.0, "order_min": 25,
     "hours": [(_ALL, "8:00", "21:00")]},
    {"name": "Milano Market", "coords": (40.8063, -73.9651), "kinds": ("food", "coffee"),
     "price": 1, "vibe": "deli, grab-and-go sandwiches", "rating": 4.0, "order_min": 4,
     "hours": [(_ALL, "6:00", "23:00")]},
    {"name": "Absolute Bagels", "coords": (40.8013, -73.9668), "kinds": ("food", "coffee"),
     "price": 1, "vibe": "grab-and-go bagels, cash only", "rating": 4.5, "order_min": 6,
     "hours": [(_ALL, "6:00", "19:00")]},
]


def _hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _open_status(hours: list, when: datetime) -> tuple[bool, int | None]:
    """(is_open, minutes_until_close) at a moment. Handles hours that run past midnight."""
    wd, now_min = when.weekday(), when.hour * 60 + when.minute
    for days, o, c in hours:
        o_min, c_min = _hm(o), _hm(c)
        overnight = c_min <= o_min
        if wd in days and now_min >= o_min:
            end = c_min + 1440 if overnight else c_min
            if now_min < end:
                return True, end - now_min
        if overnight and (wd - 1) % 7 in days and now_min < c_min:  # yesterday's late hours
            return True, c_min - now_min
    return False, None


def _station_coords(stop_id: str) -> tuple[float, float] | None:
    coords = PLACES.get(STATIONS[stop_id].lower())
    if coords:
        return coords
    try:
        hit = _resolve_place(STATIONS[stop_id] + " subway station, Manhattan")
    except requests.RequestException:
        return None
    return hit[:2] if hit else None


def coffee_before_train(start: str, direction: str, kind: str = "coffee", max_price: int = 3,
                        station: str = "116 St-Columbia University", route: str = "") -> str:
    """Cafes/food near campus the user can stop at and STILL catch a train, with when to leave."""
    if kind not in ("coffee", "food", "any"):
        return _error(f"Unknown kind '{kind}'.", "Use 'coffee', 'food' or 'any'.")
    stop_id = _find_station(station)
    if not stop_id:
        return _error(f"'{station}' isn't a 1/2/3 station I know.",
                      "Use e.g. '116 St-Columbia University' or 'Cathedral Pkwy (110 St)'.")
    d = _normalize_direction(direction)
    if not d:
        return _error(f"Need a direction, got '{direction}'.",
                      "Ask the user if they're going 'uptown' or 'downtown', then retry.")
    try:
        origin = _resolve_place(start)
    except requests.RequestException as e:
        return _error(f"Geocoding failed: {e}", "Retry with a campus building name.")
    if origin is None:
        return _error(f"Couldn't find '{start}'.", "Ask the user which building or corner they're at.")
    station_coords = _station_coords(stop_id)
    if station_coords is None:
        return _error("Can only plan stops for stations near campus.",
                      "Use 116 St, Cathedral Pkwy (110 St) or 125 St.")
    try:
        arrivals = _arrivals(stop_id, d, route)[:6]
    except Exception as e:
        return _error(f"MTA real-time feed unavailable: {e}", "Tell the user live times are unavailable.")
    if not arrivals:
        return _error("No upcoming trains in the live feed.", "Check get_subway_alerts for suspensions.")

    now_ts = time.time()
    now_dt = _now()
    _, direct_walk = _walk_minutes(origin[:2], station_coords)

    def first_catchable(total_min: float) -> int | None:
        for i, a in enumerate(arrivals):
            if (a["_ts"] - now_ts) / 60 - total_min >= 0:
                return i
        return None

    base_i = first_catchable(direct_walk + STATION_BUFFER_MIN)
    baseline = {
        "train": f"{arrivals[base_i]['route']} at {arrivals[base_i]['arrives_at']}",
        "leave_by": _clock(arrivals[base_i]["_ts"] - (direct_walk + STATION_BUFFER_MIN + 1) * 60),
    } if base_i is not None else None

    options = []
    for spot in FOOD_SPOTS:
        if (kind != "any" and kind not in spot["kinds"]) or spot["price"] > max_price:
            continue
        walk_in = _walk_minutes(origin[:2], spot["coords"])[1]
        walk_out = _walk_minutes(spot["coords"], station_coords)[1]
        total = walk_in + spot["order_min"] + walk_out + STATION_BUFFER_MIN
        arrive_dt = now_dt + timedelta(minutes=walk_in)
        is_open, left = _open_status(spot["hours"], arrive_dt)
        if not is_open or left < spot["order_min"]:
            continue  # closed, or closing before you'd be served
        i = first_catchable(total)
        if i is None:
            continue
        t = arrivals[i]
        slack = (t["_ts"] - now_ts) / 60 - total
        leave = t["_ts"] - (total + 1) * 60 if slack >= 2 else now_ts
        options.append({
            "place": spot["name"], "price": "$" * spot["price"], "vibe": spot["vibe"],
            "our_rating": spot["rating"], "closes_in_min": left if left < 120 else None,
            "walk_there_min": math.ceil(walk_in), "est_wait_min": spot["order_min"],
            "catches_train": f"{t['route']} at {t['arrives_at']}",
            "leave_by": "right now" if leave == now_ts else _clock(leave),
            "makes_next_train": i == 0,
            "extra_wait_vs_going_straight_min": (
                round((t["_ts"] - arrivals[base_i]["_ts"]) / 60) if base_i is not None else None),
            "_rank": (i, -spot["rating"], total),
        })
    options.sort(key=lambda o: o.pop("_rank"))

    result = {
        "from": origin[2], "station": STATIONS[stop_id],
        "direction": "uptown" if d == "N" else "downtown",
        "now": now_dt.strftime("%-I:%M %p"),
        "going_straight_to_station": baseline,
        "options": options[:4],
        "note": "Hours, prices and ratings are approximate (our own take); wait times are estimates.",
    }
    if not options:
        result["note"] = ("Nothing open fits before the next trains. Suggest going straight to the "
                          "station, or relax the kind/max_price filters.")
    return json.dumps(result)


# --- Sunrise / sunset ---

ARRIVE_EARLY_MIN = 15  # get there before the sky starts doing its thing
# faces = which horizon the spot looks at. Hand-picked; double-check the views in person.
SUN_SPOTS = [
    {"name": "Riverside Park (Hudson overlook at 116th)", "coords": (40.8101, -73.9692), "faces": "west",
     "note": "Open view over the Hudson toward New Jersey; the classic choice."},
    {"name": "Sakura Park", "coords": (40.8131, -73.9623), "faces": "west",
     "note": "Quieter lawn near Riverside Church; trees and buildings may clip the horizon."},
    {"name": "Grant's Tomb plaza", "coords": (40.8134, -73.9631), "faces": "west",
     "note": "Hilltop plaza next to Riverside Drive."},
    {"name": "Morningside Park (east edge)", "coords": PLACES["morningside park"], "faces": "east",
     "note": "Looks east over Harlem, the best nearby option for sunrise."},
]


def _sky_outlook(low: float, mid: float, high: float, rain: float) -> tuple[str, str]:
    """Rule of thumb: high/mid clouds catch the color, low clouds or rain block the horizon."""
    upper = max(mid, high)
    if rain >= 60:
        return "poor", f"{rain:.0f}% chance of rain around then."
    if low >= 70:
        return "poor", f"Low clouds ({low:.0f}%) will likely block the horizon."
    if low <= 40 and 30 <= upper <= 85:
        return "vivid", f"Clear horizon with {upper:.0f}% mid/high cloud to catch the color."
    if low <= 40 and upper < 30:
        return "clean but plain", "Mostly clear sky: a clean sunset but not much color."
    return "decent", f"Mixed cloud (low {low:.0f}%, mid {mid:.0f}%, high {high:.0f}%)."


def sun_spots(event: str = "sunset", start: str = "Columbia") -> str:
    """Next sunrise/sunset time, a sky-quality outlook, and walking-time to nearby viewpoints."""
    event = event.lower().strip()
    if event not in ("sunrise", "sunset"):
        return _error(f"Unknown event '{event}'.", "Use 'sunrise' or 'sunset'.")
    try:
        origin = _resolve_place(start)
    except requests.RequestException as e:
        return _error(f"Geocoding failed: {e}", "Retry with a campus building name.")
    if origin is None:
        return _error(f"Couldn't find '{start}'.", "Ask where the user is, or use 'Columbia'.")
    try:
        data = requests.get(
            FORECAST_URL,
            params={
                "latitude": origin[0], "longitude": origin[1],
                "daily": "sunrise,sunset",
                "hourly": "cloud_cover_low,cloud_cover_mid,cloud_cover_high,"
                          "precipitation_probability,apparent_temperature",
                "temperature_unit": "fahrenheit", "timezone": "America/New_York", "forecast_days": 2,
            },
            timeout=10,
        ).json()
        daily, hourly = data["daily"], data["hourly"]
    except (requests.RequestException, KeyError, ValueError) as e:
        return _error(f"Weather service failed: {e}", "Tell the user sun times are unavailable right now.")

    now = _now()
    candidates = [datetime.fromisoformat(s).replace(tzinfo=NYC) for s in daily[event]]
    ev = next((t for t in candidates if t > now), None)  # strictly in the future
    if ev is None:
        return _error(f"No upcoming {event} in the forecast.", "Tell the user and try again later.")
    hour_key = (ev + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:00")
    try:
        i = hourly["time"].index(hour_key)
    except ValueError:
        return _error("Forecast doesn't cover that hour.", "Tell the user the sky outlook is unavailable.")
    low, mid, high, rain = (hourly[k][i] or 0 for k in (
        "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high", "precipitation_probability"))
    label, why = _sky_outlook(low, mid, high, rain)

    horizon = "west" if event == "sunset" else "east"
    spots = []
    for spot in SUN_SPOTS:
        if spot["faces"] != horizon:
            continue
        walk = math.ceil(_walk_minutes(origin[:2], spot["coords"])[1])
        leave_ts = ev.timestamp() - (walk + ARRIVE_EARLY_MIN) * 60
        if leave_ts >= time.time():
            status = f"leave by {_clock(leave_ts)}" + (" tomorrow" if ev.date() > now.date() else "")
        else:
            spare = round((ev.timestamp() - time.time()) / 60 - walk)
            status = (f"leave now; you'd arrive about {spare} min before {event}" if spare > 0
                      else f"you'd arrive after {event}")
        spots.append({"place": spot["name"], "walk_min": walk, "plan": status, "note": spot["note"]})
    spots.sort(key=lambda s: s["walk_min"])

    is_tomorrow = ev.date() > now.date()
    return json.dumps({
        "now": now.strftime("%-I:%M %p"),
        "event": event, "time": ev.strftime("%-I:%M %p"),
        "day": "tomorrow" if is_tomorrow else "today",
        "minutes_away": round((ev - now).total_seconds() / 60),
        "sky_outlook": label, "why": why,
        "feels_like_f_then": hourly["apparent_temperature"][i],
        "from": origin[2], "spots": spots,
        "note": ("Outlook is a rule-of-thumb from cloud layers, not a guarantee."
                 + (f" Today's {event} has already passed, so this is tomorrow's." if is_tomorrow else "")),
    })


# --- What the model sees ---

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_next_trains",
            "description": (
                "Live MTA arrival times for the 1, 2 and 3 trains at a Broadway-7th Av line station "
                "(e.g. 116 St-Columbia University, the default). Use for 'when's the next train'. "
                "Only covers the 1/2/3 line."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "station": {"type": "string", "description": "Station name, e.g. '116 St-Columbia University', '96 St', 'Times Sq-42 St'."},
                    "direction": {"type": "string", "enum": ["uptown", "downtown", "both"], "description": "Uptown = toward the Bronx, downtown = toward Midtown/Downtown."},
                    "route": {"type": "string", "enum": ["", "1", "2", "3"], "description": "Filter to one route, or empty for all."},
                    "limit": {"type": "integer", "description": "Max arrivals to return (1-12, default 6)."},
                },
                "required": ["station"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_subway_alerts",
            "description": (
                "Active MTA service alerts (delays, suspensions, reroutes, planned weekend work) for one "
                "subway route. Call before recommending a subway trip, or when trains seem missing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "route": {"type": "string", "description": "Subway route letter or number, e.g. '1', '2', 'A', 'L'. Buses aren't supported."},
                },
                "required": ["route"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "estimate_walk",
            "description": (
                "Walking distance and time (and jogging time) between two places. Knows Columbia/Barnard "
                "buildings by name (Butler, Lerner, John Jay, Mudd, SIPA, Pupin, Barnard, Manhattanville...) "
                "and can geocode other NYC addresses."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {"type": "string", "description": "Starting place, e.g. 'Butler Library' or '350 W 110th St'."},
                    "destination": {"type": "string", "description": "Destination, e.g. 'Mudd' or 'Hungarian Pastry Shop'."},
                },
                "required": ["origin", "destination"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "catch_the_train",
            "description": (
                "Combines walking time from where the user is with live arrivals to say, for each of the "
                "next trains, whether they can walk, must run, or will miss it, and when to leave. Use when "
                "the user asks 'can I make the next train' or 'when should I leave'. Needs a direction; ask "
                "the user if unclear."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start": {"type": "string", "description": "Where the user is now, e.g. 'Mudd' or 'John Jay'."},
                    "direction": {"type": "string", "enum": ["uptown", "downtown"]},
                    "station": {"type": "string", "description": "Station to board at. Default '116 St-Columbia University'; '125 St' and 'Cathedral Pkwy (110 St)' also work."},
                    "route": {"type": "string", "enum": ["", "1", "2", "3"], "description": "Optional route filter."},
                },
                "required": ["start", "direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Current temperature, feels-like, wind, and max rain chance over the next 4 hours near a NYC place. Use for umbrella / walk-vs-train decisions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "place": {"type": "string", "description": "A campus building or NYC place. Default 'Columbia'."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "coffee_before_train",
            "description": (
                "Finds cafes and food spots near the user that are open and that they can stop at and STILL "
                "catch a train, with when to leave and how much extra waiting the stop costs. Use for 'can I "
                "grab coffee before my train' or 'food on the way to the subway'. Needs where the user is and a "
                "direction; ask if unclear. Prices, hours and ratings are approximate."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start": {"type": "string", "description": "Where the user is now, e.g. 'Butler' or 'Mudd'."},
                    "direction": {"type": "string", "enum": ["uptown", "downtown"]},
                    "kind": {"type": "string", "enum": ["coffee", "food", "any"], "description": "What they want. Default 'coffee'."},
                    "max_price": {"type": "integer", "description": "Max price tier 1-3 ($ to $$$). Default 3."},
                    "station": {"type": "string", "description": "Station to board at. Default '116 St-Columbia University'."},
                    "route": {"type": "string", "enum": ["", "1", "2", "3"], "description": "Optional route filter."},
                },
                "required": ["start", "direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sun_spots",
            "description": (
                "Next sunrise or sunset time at Columbia, a sky-quality outlook from cloud cover and rain "
                "chance (vivid / decent / clean but plain / poor), the feels-like temperature then, and nearby "
                "viewpoints with walk time and when to leave. Use for 'where can I watch the sunset', "
                "'is the sunset going to be good', or sunrise questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "event": {"type": "string", "enum": ["sunset", "sunrise"], "description": "Default 'sunset'."},
                    "start": {"type": "string", "description": "Where the user is now, e.g. 'Butler'. Default 'Columbia'."},
                },
                "required": [],
            },
        },
    },
]

# What the harness runs: tool name -> Python function.
TOOL_MAP = {
    "get_next_trains": get_next_trains,
    "get_subway_alerts": get_subway_alerts,
    "estimate_walk": estimate_walk,
    "catch_the_train": catch_the_train,
    "get_weather": get_weather,
    "coffee_before_train": coffee_before_train,
    "sun_spots": sun_spots,
}


def run_tool(name: str, args: dict) -> str:
    """Run one tool call. Models invent tool names and arguments; never let that crash the loop."""
    if name not in TOOL_MAP:
        return _error(f"Unknown tool '{name}'.", f"Available tools: {list(TOOL_MAP)}")
    try:
        return TOOL_MAP[name](**args)
    except TypeError as e:
        return _error(f"Bad arguments for {name}: {e}", "Check the tool's parameter names and retry.")
    except Exception as e:  # last line of defense: a bug in a tool shouldn't 500 the chat
        return _error(f"{name} failed unexpectedly: {type(e).__name__}: {e}")
