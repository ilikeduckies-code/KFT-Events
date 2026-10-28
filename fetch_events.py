import json, os, sys, requests
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
    # the list lives under some key that contains "edges"; find it without guessing the name
    if isinstance(obj, dict):
        if "edges" in obj:
            return obj
        for v in obj.values():
            found = find_connection(v)
            if found:
                return found
    return None

edges, seen, after = [], set(), None
for _ in range(MAX_PAGES):
    conn = find_connection(fetch_page(after))
    if not conn or not conn["edges"]:
        break
    new = [e for e in conn["edges"] if e["node"]["tournament"]["id"] not in seen]
    if not new:
        break  # cursor not honored; avoid looping forever
    for e in new:
        seen.add(e["node"]["tournament"]["id"])
    edges += new
    info = conn.get("pageInfo") or {}
    if info.get("hasNextPage") is False or len(conn["edges"]) < PAGE_SIZE:
        break
    after = info.get("endCursor") or conn["edges"][-1].get("cursor")

if not edges:
    sys.exit("No events came back from the API; leaving events.json unchanged.")

events = []
for e in edges:
    n = e["node"]
    if n["organizer"]["id"] != ORGANIZER_ID:
        continue
    t = n["tournament"]
    cfg = t.get("config") or {}
    registered = sum(c["count"] for c in t.get("registrantCounts", []) if c["status"] == "REGISTERED")
    start = datetime.fromisoformat(t["startsAt"].replace("Z", "+00:00"))
    price = "Free" if t.get("pricing") == "FREE" else (t.get("entryFee") or t.get("pricing"))
    events.append({
        "name": t["name"],
        "start": t["startsAt"],
        "startLocal": start.astimezone(TZ).strftime("%a %b %d, %-I:%M %p"),
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

if "--discord" in sys.argv and os.environ.get("DISCORD_WEBHOOK") and events:
    lines = [
        f"**{e['name']}**\n{e['startLocal']} | {e['price'] or 'See event'} | {e['registered']}/{e['capacity']} registered\n<{e['url']}>"
        for e in events[:10]
    ]
    requests.post(
        os.environ["DISCORD_WEBHOOK"],
        json={"content": "Upcoming Riftbound events at Kung Fu Tea Huntington Hills:\n\n" + "\n\n".join(lines)},
        timeout=30,
    )