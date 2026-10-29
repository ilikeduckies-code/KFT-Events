import hashlib, json, os, sys, time, requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ORGANIZER_ID = "01a11d4d-6e30-7436-9b8e-c2a2966ebff6"   # Kung Fu Tea Huntington Hills
LAT, LNG = 51.1566, -114.0568
RADIUS_M = 32187
PAGE_SIZE = 20
MAX_PAGES = 15
API = "https://playriftbound.com/api/gql"
HASH = "868030f676276bec926c740aada7129713d4097f6e2dd7c3a0451c4fb43852eb"
TZ = ZoneInfo("America/Edmonton")
STATE_FILE = "state.json"
SIG_KEYS = ["name", "start", "price", "capacity", "format", "type", "url"]
MISSES_BEFORE_CANCELLED = 2

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "KFT-Huntington-Events/1.0"})

def warm_up():
    r = SESSION.get("https://playriftbound.com/en-US/events",
                    params={"lat": LAT, "lng": LNG}, timeout=30)
    print("Warm-up status:", r.status_code, "| cookies received:", list(SESSION.cookies.keys()))

def fetch_page(after=None):
    variables = {
        "sport": "rb",
        "first": PAGE_SIZE,
        "filter": {"rb": {
            "coords": {"latitude": LAT, "longitude": LNG},
            "startDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "distanceMeters": RADIUS_M,
        }},
        "sortBy": {"rb": "DATE"},
    }
    if after:
        variables["after"] = after
    extensions = {
        "clientLibrary": {"name": "@apollo/client", "version": "4.1.9"},
        "persistedQuery": {"version": 1, "sha256Hash": HASH},
    }
    r = SESSION.get(
        API,
        params={
            "operationName": "CompeteTournamentSearch",
            "variables": json.dumps(variables, separators=(",", ":")),
            "extensions": json.dumps(extensions, separators=(",", ":")),
        },
        headers={
            "apollo-require-preflight": "true",
            "x-apollo-operation-name": "CompeteTournamentSearch",
            "Accept": "application/json",
            "Referer": "https://playriftbound.com/en-US/events",
            "Origin": "https://playriftbound.com",
        },
        timeout=30,
    )
    if r.status_code >= 400:
        print("API refused:", r.status_code,
              "| www-authenticate:", r.headers.get("www-authenticate"),
              "| body:", r.text[:300])
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(f"API returned errors: {body['errors']}")
    return body["data"]

def find_connection(obj):
    if isinstance(obj, dict):
        if "edges" in obj:
            return obj
        for v in obj.values():
            found = find_connection(v)
            if found:
                return found
    return None

def pretty(s):
    return s.replace("_", " ").title() if s else None

def parse_utc(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

# ---------- Discord helpers ----------

def call(method, url, **kw):
    r = requests.request(method, url, timeout=30, **kw)
    if r.status_code == 429:
        time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
        r = requests.request(method, url, timeout=30, **kw)
    return r

def send(hook, payload, mid=None):
    """Edit message mid if it exists, otherwise post a new one. Returns the message id."""
    if mid:
        r = call("PATCH", f"{hook}/messages/{mid}", json=payload)
        if r.status_code != 404:
            r.raise_for_status()
            return mid
    r = call("POST", hook, params={"wait": "true"}, json=payload)
    r.raise_for_status()
    return r.json()["id"]

def delete_msg(hook, mid):
    r = call("DELETE", f"{hook}/messages/{mid}")
    if r.status_code not in (200, 204, 404):
        r.raise_for_status()

def event_embed(e):
    fields = [{"name": "When", "value": e["startLocal"], "inline": True},
              {"name": "Price", "value": e["price"] or "See event page", "inline": True}]
    if e.get("capacity"):
        fields.append({"name": "Spots", "value": str(e["capacity"]), "inline": True})
    extra = " | ".join(x for x in [pretty(e.get("type")), pretty(e.get("format"))] if x)
    return {
        "title": e["name"],
        "url": e["url"],
        "description": (extra + "\n" if extra else "") + f"[Details and sign up]({e['url']})",
