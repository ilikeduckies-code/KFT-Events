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
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "KFT-Huntington-Events/1.0"})

def warm_up():
    r = SESSION.get("https://playriftbound.com/en-US/events",
                    params={"lat": LAT, "lng": LNG}, timeout=30)
    print("Warm-up status:", r.status_code, "| cookies received:", list(SESSION.cookies.keys()))
STATE_FILE = "state.json"
SIG_KEYS = ["name", "start", "price", "capacity", "format", "type", "url"]
MISSES_BEFORE_CANCELLED = 2

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
    r = requests.get(
        API,
        params={
            "operationName": "CompeteTournamentSearch",
            "variables": json.dumps(variables, separators=(",", ":")),
            "extensions": json.dumps(extensions, separators=(",", ":")),
        },
        headers={
            "User-Agent": "KFT-Huntington-Events/1.0",
            "apollo-require-preflight": "true",
            "x-apollo-operation-name": "CompeteTournamentSearch",
            "Accept": "application/json",
        },
        timeout=30,
    )
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
        "color": 0x2ECC71,
        "fields": fields,
        "footer": {"text": "Kung Fu Tea Huntington Hills"},
    }

def cancelled_embed(name, url):
    return {
        "title": f"{name}",
        "url": url,
        "description": "This event is no longer listed. It may have been cancelled or changed. Check the event page or ask in the channel.",
        "color": 0xE74C3C,
        "footer": {"text": "Kung Fu Tea Huntington Hills"},
    }

def discord_sync(events, state):
    hook = os.environ.get("DISCORD_WEBHOOK")
    if not hook:
        print("No DISCORD_WEBHOOK set; skipping Discord")
        return state
    known = state.get("events", {})
    current = {e["id"]: e for e in events}
    new_state, now = {}, datetime.now(timezone.utc)
    posted = edited = deleted = cancelled = 0

    for eid, e in current.items():
        sig = hashlib.sha256(json.dumps({k: e[k] for k in SIG_KEYS}, sort_keys=True).encode()).hexdigest()
        prev = known.get(eid, {})
        mid = prev.get("message_id")
        keep = {"name": e["name"], "url": e["url"], "start": e["start"]}
        if mid and prev.get("hash") == sig and not prev.get("cancelled"):
            new_state[eid] = {**keep, "hash": sig, "message_id": mid, "missing": 0}
            continue
        new_mid = send(hook, {"embeds": [event_embed(e)]}, mid)
        posted += 0 if mid and new_mid == mid else 1
        edited += 1 if mid and new_mid == mid else 0
        new_state[eid] = {**keep, "hash": sig, "message_id": new_mid, "missing": 0}
        time.sleep(1)

    for eid, prev in known.items():
        if eid in current:
            continue
        mid = prev.get("message_id")
        if parse_utc(prev["start"]) < now:
            if mid:
                delete_msg(hook, mid)
                deleted += 1
                time.sleep(1)
            continue   # drop from state
        prev["missing"] = prev.get("missing", 0) + 1
        if prev["missing"] >= MISSES_BEFORE_CANCELLED and not prev.get("cancelled") and mid:
            send(hook, {"embeds": [cancelled_embed(prev["name"], prev["url"])]}, mid)
            prev["cancelled"] = True
            cancelled += 1
            time.sleep(1)
        new_state[eid] = prev

    print(f"Discord: {posted} posted, {edited} edited, {deleted} deleted, {cancelled} marked cancelled")
    return {"events": new_state}

# ---------- Main ----------

edges, seen, after = [], set(), None
for _ in range(MAX_PAGES):
    conn = find_connection(fetch_page(after))
    if not conn or not conn["edges"]:
        break
    new = [e for e in conn["edges"] if e["node"]["tournament"]["id"] not in seen]
    if not new:
        break
    for e in new:
        seen.add(e["node"]["tournament"]["id"])
    edges += new
    info = conn.get("pageInfo") or {}
    if info.get("hasNextPage") is False or len(conn["edges"]) < PAGE_SIZE:
        break
    after = info.get("endCursor") or conn["edges"][-1].get("cursor")

if not edges:
    sys.exit("No events came back from the API; leaving everything unchanged.")

events = []
for e in edges:
    n = e["node"]
    if n["organizer"]["id"] != ORGANIZER_ID:
        continue
    t = n["tournament"]
    cfg = t.get("config") or {}
    registered = sum(c["count"] for c in t.get("registrantCounts", []) if c["status"] == "REGISTERED")
    price = "Free" if t.get("pricing") == "FREE" else (t.get("entryFee") or t.get("pricing"))
    events.append({
        "id": t["id"],
        "name": t["name"],
        "start": t["startsAt"],
        "startLocal": parse_utc(t["startsAt"]).astimezone(TZ).strftime("%a %b %d, %-I:%M %p"),
        "price": str(price) if price else None,
        "registered": registered,
        "capacity": cfg.get("participantCapacity"),
        "format": cfg.get("format"),
        "type": cfg.get("tournamentType"),
        "url": f"https://playriftbound.com/en-US/events/{t['id']}",
    })

events.sort(key=lambda x: x["start"])
with open("events.json", "w") as f:
    json.dump(events, f, indent=2)
print(f"Saved {len(events)} Kung Fu Tea events")

try:
    with open(STATE_FILE) as f:
        state = json.load(f)
except Exception:
    state = {}
state = discord_sync(events, state)
with open(STATE_FILE, "w") as f:
    json.dump(state, f, indent=2)
