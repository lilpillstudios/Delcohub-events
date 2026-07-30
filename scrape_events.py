#!/usr/bin/env python3
"""
DelcoHub Event Scraper v1.0
Lil Pill Studios (c) 2026

Architecture:
  Delco Times events calendar (CitySpark portal API) -> raw occurrences
  Delaware County boundary polygon                   -> county filter
  Occurrence grouping                                -> one listing per event run
  manual_events.json (Z's curated, protected)        -> merged on top
       |
  Dedup (manual wins) + drop past + sort by date -> events.json

Output is a DelcoHub listing array (category "events"), so the app can drop it
straight into the dataset. See ../DelcoHub/src/data/schema.json.

The Delco Times calendar is a CitySpark portal. Its public read API takes an
unauthenticated JSON POST; the query is a radius around a point, so everything
inside the radius but outside the county (Philadelphia, Wilmington, Chester and
Montgomery counties, South Jersey) is discarded by point-in-polygon against the
Census boundary in delco_boundary.json.
"""

import argparse
import hashlib
import json
import math
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent

# ---- CitySpark portal (Delco Times) -----------------------------------------
API = "https://portal.cityspark.com/api/events/GetEvents/DelcoTimes"
PPID = 9409                       # Delco Times portal id, from the embed script
CENTER_LAT, CENTER_LNG = 39.907793, -75.3878525   # portal default: Delaware County, PA
RADIUS_MI = 15                    # must cover the whole county (asserted at startup)
PAGE = 100                        # page size the API returns when `end` is set
SKIP_CAP = 2000                   # API returns nothing past ~2025 results per query
WINDOW_DAYS = 7                   # date slice per query, to stay under SKIP_CAP
REQUEST_PAUSE = 0.25

HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://www.delcotimes.com",
    "Referer": "https://www.delcotimes.com/events/",
    "User-Agent": "DelcoHub-EventScraper/1.0 (Lil Pill Studios; lilpillstudios@gmail.com)",
}

# ---- CitySpark tag id -> DelcoHub subcategory --------------------------------
# Only the tags worth surfacing; anything unmapped is dropped from subcategories.
TAG_SUB = {
    2: "performing-arts", 3: "visual-arts", 4: "literary-arts", 5: "destinations",
    6: "sports-outdoors", 7: "learning", 10: "lifestyle", 11: "civic",
    12: "food-drink", 13: "ongoing", 14: "nightlife", 15: "special-audience",
    16: "arts", 17: "music", 18: "dance", 19: "theater", 20: "comedy", 21: "open-mic",
    26: "film", 31: "festival", 32: "museums-exhibits", 33: "animals",
    34: "parks-gardens", 35: "sightseeing", 36: "sports", 37: "outdoor-recreation",
    38: "fitness", 39: "workshop", 40: "talks-lectures", 41: "classes",
    51: "crafts", 53: "games", 54: "markets", 60: "health-wellness",
    61: "faith", 63: "multicultural", 68: "volunteering", 69: "fundraiser",
    74: "food", 75: "drinks", 77: "bars", 79: "family", 80: "kids",
    100: "concert", 118: "musical", 166: "nature", 167: "running",
    189: "author-event", 190: "attraction", 195: "farms", 385: "farmers-market",
    386: "flea-market", 390: "holiday", 400: "halloween", 403: "christmas",
    10036: "haunted-house", 10037: "pumpkin-patch", 10038: "fireworks",
    10051: "street-fair", 10052: "parade", 10054: "carnival",
    10092: "outdoor-movie", 10138: "outdoor-concert", 10149: "food-trucks",
    10165: "trivia", 10174: "oktoberfest", 10212: "juneteenth",
    10222: "comic-con", 10252: "beer", 10257: "holiday-market",
    10262: "live-music", 10265: "pickleball", 10270: "craft-beer",
    10284: "food-tour", 10298: "stand-up", 10300: "summer-camp",
}

# CitySpark tag id -> DelcoHub `tags` vocabulary (schema.json: tags)
TAG_FLAG = {
    79: "family", 80: "kid-friendly", 15: "family",
    34: "outdoor", 37: "outdoor", 166: "outdoor", 31: "outdoor",
    14: "open-late", 77: "open-late",
    32: "historic", 35: "historic", 428: "historic",
    36: "group", 6: "group",
}

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# ---- noise ------------------------------------------------------------------
# DelcoHub is a "what should I do today" app, not a civic notice board. The
# Delco Times calendar carries township meetings, networking mixers and realtor
# open houses; none of those belong in Explore.
NOISE_TAGS = {
    8,      # Professional
    42,     # Business
    43,     # Real Estate
    45,     # Law
    48,     # Jobs & Career
    49,     # Networking
    70,     # Politics & Government
    172, 173, 174, 175, 176, 177, 178,   # HR / finance / sales / expos
    439,    # Government Meetings
    914, 915, 916,                       # committee / townhall / city council
    10005,  # Elections
    10251,  # Real Estate Open House
}
NOISE_TITLE = re.compile(
    r"\b(board|commission|committee|council|authority|supervisors|trustees)\s+"
    r"(meeting|session|hearing)\b"
    r"|\b(public hearing|town ?hall meeting|zoning hearing|budget hearing)\b"
    r"|\bregistration\s+(opens?|closes?|deadline|now open)\b"
    r"|\b(best of|readers'? choice|vote now|nominate)\b"
    r"|\bopen house\b.*\b(realty|real estate|listing)\b"
    r"|\b(networking|mixer)\b.*\b(chamber|business)\b",
    re.I,
)


# Private schools publish their whole internal calendar to the Delco Times feed
# (Episcopal Academy alone accounted for ~40 entries: chapel, photo day, dress
# code days, dismissals). Those are not things to do in Delco. Public-facing
# school events — fairs, markets, plays, college athletics — are kept.
SCHOOL_NOISE = re.compile(
    r"^(LS|MS|US|JK|SK|PreK|PK)\b"
    r"|\b(dress[- ]down|special dress|photo day|early dismissal|dismissal|no school"
    r"|report cards?|advisory|homeroom|chapel|vespers?|convocation"
    r"|back[- ]to[- ]school (night|webinar)|parents?'? (forum|reception|conference)s?"
    r"|faculty (meeting|exhibition)|class of '\d+|spirit week|senior class"
    r"|open house|admissions?|tour)\b",
    re.I,
)
# ...unless the title reads as something the public would actually turn up for.
SCHOOL_KEEP = re.compile(
    r"\b(farmers? market|festival|fair|concert|craft brew|5k|run|race|musical"
    r"|play|theatre|theater|exhibit|gallery|book sale|thrift|bbq|family fun"
    r"|vs\.?|invitational|championship|tournament)\b",
    re.I,
)


# Awareness days and religious observances get posted as all-day "events" with
# nowhere to go. "Yom Kippur" is not a plan; "Yom Kippur Dinner" is.
OBSERVANCE = re.compile(
    r"^\s*("
    r"(national|world|international|global)\s+\w+(\s+\w+)*\s+(day|month|week)"
    r"|rosh hashanah|yom kippur|eid[\w\s-]*|diwali|hanukkah|chanukah|passover"
    r"|ash wednesday|good friday|palm sunday|lent|ramadan|purim|sukkot"
    r"|lgbtq\+? history month|black history month|pride month"
    r"|hispanic heritage month|indigenous peoples'? day|juneteenth"
    r")\s*$",
    re.I,
)


def load_blocked_venues():
    """Venues that dump an internal calendar into the public feed.

    Kept as data, not code, so a noisy venue can be silenced with a one-line
    edit instead of a regex change. Matched on normalized venue name.
    """
    p = HERE / "blocked_venues.json"
    if not p.exists():
        return set()
    return {norm_name(v) for v in json.loads(p.read_text(encoding="utf-8")).get("venues", [])}


BLOCKED_VENUES = None


def is_noise(e):
    global BLOCKED_VENUES
    if BLOCKED_VENUES is None:
        BLOCKED_VENUES = load_blocked_venues()
    name = e.get("Name") or ""
    if set(e.get("Tags") or []) & NOISE_TAGS:
        return True
    if NOISE_TITLE.search(name):
        return True
    if OBSERVANCE.match(name.strip()):
        return True
    if SCHOOL_NOISE.search(name) and not SCHOOL_KEEP.search(name):
        return True
    if norm_name(clean_venue(e.get("Venue"))) in BLOCKED_VENUES:
        return True
    return False


# ---- geometry ----------------------------------------------------------------
def load_boundary():
    ring = json.loads((HERE / "delco_boundary.json").read_text())["ring"]
    return [(float(x), float(y)) for x, y in ring]


def in_polygon(lng, lat, ring):
    """Standard ray-cast point-in-polygon. ring is [(lng, lat), ...]."""
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > lat) != (y2 > lat):
            if lng < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
                inside = not inside
    return inside


def miles(lat1, lng1, lat2, lng2):
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def assert_radius_covers_county(ring):
    """The radius query must not clip the county. Fail loudly if it would."""
    worst = max(miles(CENTER_LAT, CENTER_LNG, lat, lng) for lng, lat in ring)
    if worst > RADIUS_MI:
        raise SystemExit(
            f"RADIUS_MI={RADIUS_MI} does not cover the county "
            f"(farthest boundary point is {worst:.1f} mi). Raise RADIUS_MI."
        )
    return worst


# ---- fetching ----------------------------------------------------------------
def post(body, retries=3):
    data = json.dumps(body).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(API, data=data, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            if attempt == retries - 1:
                print(f"  REQUEST FAILED after {retries}: {e}", file=sys.stderr)
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def fetch_window(start, end, verbose=False):
    """All occurrences in [start, end). Returns a list of raw CitySpark events."""
    out, skip = [], 0
    while skip < SKIP_CAP:
        body = {
            "ppid": PPID, "start": f"{start}T00:00:00Z", "end": f"{end}T00:00:00Z",
            "labels": [], "pick": False, "tps": None, "sparks": False, "sort": "Time",
            "category": [], "distance": RADIUS_MI, "lat": CENTER_LAT, "lng": CENTER_LNG,
            "search": "", "skip": skip, "defFilter": "all",
        }
        resp = post(body)
        if not resp or not resp.get("Success"):
            break
        batch = resp.get("Value") or []
        if not batch:
            break
        out.extend(batch)
        skip += len(batch)
        if len(batch) < PAGE:
            break
        time.sleep(REQUEST_PAUSE)
    if skip >= SKIP_CAP:
        print(f"  WARNING: window {start}..{end} hit the {SKIP_CAP}-result API cap; "
              f"some events in this window were not seen. Lower WINDOW_DAYS.",
              file=sys.stderr)
    if verbose:
        print(f"  [{start} -> {end}] {len(out)} raw")
    return out


def scrape(horizon_days, verbose=False):
    ring = load_boundary()
    worst = assert_radius_covers_county(ring)
    if verbose:
        print(f"[boundary] {len(ring)} points, farthest {worst:.1f} mi "
              f"(radius {RADIUS_MI} mi) OK")

    today = date.today()
    seen_pids, raw, dropped, noise = set(), [], 0, 0
    cursor = today
    horizon = today + timedelta(days=horizon_days)
    while cursor < horizon:
        nxt = min(cursor + timedelta(days=WINDOW_DAYS), horizon)
        for e in fetch_window(cursor.isoformat(), nxt.isoformat(), verbose):
            lat, lng = e.get("latitude"), e.get("longitude")
            if not lat or not lng:
                dropped += 1
                continue
            if not in_polygon(lng, lat, ring):
                dropped += 1
                continue
            if is_noise(e):
                noise += 1
                continue
            # PId repeats across occurrences; key on PId+date to keep each date once
            key = (e.get("PId"), (e.get("DateStart") or "")[:10])
            if key in seen_pids:
                continue
            seen_pids.add(key)
            raw.append(e)
        cursor = nxt
        time.sleep(REQUEST_PAUSE)

    print(f"  Raw occurrences in Delaware County: {len(raw)}  "
          f"(dropped out-of-county: {dropped}, civic/business noise: {noise})")
    return raw


# ---- mapping -----------------------------------------------------------------
def clean_text(s):
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    # Submitters paste markdown-escaped copy: "\(90 minutes\)", "55\+".
    s = re.sub(r"\\([^A-Za-z0-9\s])", r"\1", s)
    s = re.sub(r"[*_#`]+", "", s)
    # Some source rows arrive with smart quotes already mangled to U+FFFD.
    # Between two letters that was an apostrophe; elsewhere it was a quote mark.
    s = re.sub(r"(?<=[A-Za-z])\ufffd(?=[A-Za-z])", "'", s)
    s = s.replace("\ufffd", "")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def clean_venue(s):
    """Some rows carry a stray state fragment: 'Pa., Edith R. Dixon Field'."""
    s = clean_text(s)
    s = re.sub(r"^(pa|pennsylvania|de|nj)\.?,\s*", "", s, flags=re.I)
    return s.strip(" ,")


def short_desc(e):
    """One-sentence excerpt. CitySpark's `Short` is boilerplate, so prefer Description."""
    body = clean_text(e.get("Description"))
    if len(body) >= 25:
        if len(body) <= 170:
            return body
        # Prefer a whole first sentence; otherwise truncate on a word boundary.
        m = re.match(r"(.{40,170}?[.!?])(\s|$)", body)
        return m.group(1) if m else body[:170].rsplit(" ", 1)[0] + "..."
    venue = clean_venue(e.get("Venue")) or "a Delco venue"
    town = (e.get("CityState") or "").split(",")[0].strip()
    return f"{clean_text(e.get('Name'))} at {venue}{(' in ' + town) if town else ''}."


def price_tier(e):
    hi = e.get("PriceHigh") or e.get("Price") or 0
    try:
        hi = float(hi)
    except (TypeError, ValueError):
        hi = 0
    if hi <= 0:
        return 0
    if hi <= 15:
        return 1
    if hi <= 45:
        return 2
    return 3


def slug(s, n=48):
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s[:n].strip("-")


def norm_name(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def fmt_day(d):
    return d.strftime("%b ") + str(d.day)


def describe_run(dates):
    """(recurring, schedule label) for a sorted list of date objects."""
    first, last = dates[0], dates[-1]
    if len(dates) == 1:
        return "none", f"{first.strftime('%a')}, {fmt_day(first)}"

    span = (last - first).days + 1
    weekdays = {d.weekday() for d in dates}
    gaps = {(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)}

    if gaps == {1}:
        return ("seasonal" if span > 45 else "none"), f"Daily, {fmt_day(first)} - {fmt_day(last)}"
    if len(weekdays) == 1:
        day = WEEKDAYS[dates[0].weekday()] + "s"
        return "weekly", f"{day}, {fmt_day(first)} - {fmt_day(last)}"
    if len(weekdays) <= 3 and gaps and max(gaps) <= 7:
        days = ", ".join(WEEKDAYS[w][:3] for w in sorted(weekdays))
        return "weekly", f"{days}, {fmt_day(first)} - {fmt_day(last)}"
    if span > 45:
        return "seasonal", f"Select dates, {fmt_day(first)} - {fmt_day(last)}"
    return "none", f"{len(dates)} dates, {fmt_day(first)} - {fmt_day(last)}"


def build_listing(group, today, venue_index):
    """Collapse one event's occurrences into a single DelcoHub listing."""
    group.sort(key=lambda e: e["DateStart"][:10])
    lead = max(group, key=lambda e: len(clean_text(e.get("Description")) or ""))
    dates = sorted({datetime.strptime(e["DateStart"][:10], "%Y-%m-%d").date() for e in group})
    upcoming = [d for d in dates if d >= today]
    if not upcoming:
        return None

    recurring, schedule = describe_run(dates)
    tag_ids = set()
    for e in group:
        tag_ids.update(e.get("Tags") or [])

    subs = sorted({TAG_SUB[t] for t in tag_ids if t in TAG_SUB})[:4]
    flags = {TAG_FLAG[t] for t in tag_ids if t in TAG_FLAG}
    tier = min(price_tier(e) for e in group)
    if tier == 0:
        flags.add("free")
    flags.add("group")

    town = (lead.get("CityState") or "").split(",")[0].strip()
    venue = clean_venue(lead.get("Venue"))
    website = ""
    for e in group:
        for link in (e.get("Links") or []):
            if link.get("url"):
                website = link["url"]
                break
        if website:
            break

    ident = "ev-" + slug(f"{town}-{lead.get('Name')}") + "-" + \
        hashlib.md5(f"{norm_name(lead.get('Name'))}|{norm_name(venue)}".encode()).hexdigest()[:6]

    return {
        "id": ident,
        "name": clean_text(lead.get("Name"))[:120],
        "category": "events",
        "subcategories": subs,
        "shortDescription": short_desc(lead),
        "address": ", ".join(x for x in [venue, lead.get("CityState")] if x),
        "town": town,
        "lat": round(float(lead["latitude"]), 6),
        "lng": round(float(lead["longitude"]), 6),
        "priceTier": tier,
        "tags": sorted(flags),
        "hours": {},
        "startDate": dates[0].isoformat(),
        "endDate": dates[-1].isoformat() if len(dates) > 1 else "",
        "recurring": recurring,
        "venueId": venue_index.get(norm_name(venue), ""),
        "phone": clean_text(lead.get("VenuePhone")),
        "website": website,
        "images": [],
        "imageUrl": lead.get("MediumImg") or "",
        "featured": False,
        "localsGuide": False,
        "source": "delcotimes.com",
        "needsVerify": False,
        "schedule": schedule,
        "nextDate": upcoming[0].isoformat(),
        # Every remaining date this event runs, so the app's calendar can mark
        # the right days instead of guessing a span from start/end. Capped —
        # a daily attraction running all year would otherwise bloat the feed.
        "dates": [d.isoformat() for d in upcoming[:120]],
        "occurrences": len(dates),
    }


def load_venue_index(listings_path):
    """normalized venue name -> listing id, so events can point at their venue."""
    idx = {}
    if not listings_path:
        return idx
    p = Path(listings_path)
    if not p.exists():
        print(f"  [venues] {p} not found, skipping venueId matching", file=sys.stderr)
        return idx
    for l in json.loads(p.read_text(encoding="utf-8")).get("listings", []):
        if l.get("category") != "events":
            idx[norm_name(l.get("name"))] = l["id"]
    return idx


# ---- manual events -----------------------------------------------------------
def roll_forward(ev, today):
    """Keep curated recurring events alive: advance a stale nextDate by its cycle."""
    nd = ev.get("nextDate") or ""
    if not nd:
        return ev
    try:
        d = datetime.strptime(nd, "%Y-%m-%d").date()
    except ValueError:
        return ev
    if d >= today:
        return ev
    rec = ev.get("recurring", "none")
    if rec == "weekly":
        while d < today:
            d += timedelta(days=7)
    elif rec == "monthly":
        while d < today:
            d += timedelta(days=28)
    elif rec in ("annual", "seasonal"):
        while d < today:
            try:
                d = d.replace(year=d.year + 1)
            except ValueError:      # Feb 29
                d = d.replace(year=d.year + 1, day=28)
    else:
        return ev               # one-off in the past: caller drops it
    ev = dict(ev)
    ev["nextDate"] = d.isoformat()
    return ev


def load_manual(today, verbose=False):
    p = HERE / "manual_events.json"
    if not p.exists():
        return []
    out = []
    for ev in json.loads(p.read_text(encoding="utf-8")):
        ev = roll_forward(dict(ev), today)
        # Keep every record the same shape as the scraped ones.
        ev.setdefault("venueId", "")
        ev.setdefault("imageUrl", "")
        ev.setdefault("occurrences", 1)
        # Curated entries describe a pattern ("Saturdays, June-October") rather
        # than a date list, so the calendar only knows their next occurrence.
        ev.setdefault("dates", [ev["nextDate"]] if ev.get("nextDate") else [])
        ev["protected"] = True
        out.append(ev)
    if verbose:
        print(f"[manual] loaded {len(out)}")
    return out


# ---- assemble ----------------------------------------------------------------
def dedup(events):
    """Manual/curated entries win over scraped ones with the same name+town."""
    by_key = {}
    for ev in events:
        key = norm_name(ev["name"])[:40] + "|" + norm_name(ev.get("town"))
        cur = by_key.get(key)
        if cur is None:
            by_key[key] = ev
        elif ev.get("protected") and not cur.get("protected"):
            by_key[key] = ev
        elif cur.get("protected"):
            continue
        elif len(ev.get("shortDescription", "")) > len(cur.get("shortDescription", "")):
            by_key[key] = ev
    return list(by_key.values())


def drop_past(events, today):
    """No stale events. A dated event survives only if it still has a future date."""
    iso = today.isoformat()
    kept = []
    for ev in events:
        nd, ed = ev.get("nextDate") or "", ev.get("endDate") or ""
        if nd and nd >= iso:
            kept.append(ev)
        elif ed and ed >= iso:
            kept.append(ev)
        elif not nd and not ed and ev.get("recurring") in ("weekly", "monthly", "annual", "seasonal"):
            kept.append(ev)     # undated recurring: schedule label carries it
    return kept


def main():
    ap = argparse.ArgumentParser(description="DelcoHub Event Scraper v1.0")
    ap.add_argument("--output", "-o", default="events.json")
    ap.add_argument("--horizon-days", type=int, default=180,
                    help="how far ahead to scrape (default 180)")
    ap.add_argument("--listings", default="../DelcoHub/src/data/listings.json",
                    help="listings.json, used to resolve venueId")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print("=== DelcoHub Event Scraper v1.0 ===\n")
    today = date.today()

    venue_index = load_venue_index(args.listings)
    if args.verbose:
        print(f"[venues] {len(venue_index)} venues available for matching")

    raw = scrape(args.horizon_days, args.verbose)

    groups = {}
    for e in raw:
        groups.setdefault(
            (norm_name(e.get("Name")), norm_name(e.get("Venue"))), []
        ).append(e)
    print(f"  Grouped into distinct events: {len(groups)}")

    scraped = [x for x in (build_listing(g, today, venue_index) for g in groups.values()) if x]
    print(f"  Scraped listings: {len(scraped)}")

    manual = load_manual(today, args.verbose)
    print(f"  Manual (curated): {len(manual)}")

    merged = dedup(scraped + manual)
    print(f"  After dedup: {len(merged)}")
    future = drop_past(merged, today)
    print(f"  Upcoming only: {len(future)}")

    future.sort(key=lambda e: (e.get("nextDate") or "9999-99-99", e["name"]))

    if args.dry_run:
        print("\n=== DRY RUN ===")
        for e in future[:80]:
            mark = " *" if e.get("protected") else ""
            print(f"  {e.get('nextDate','----------')} | {e['name'][:52]:52s} | "
                  f"{e.get('town','')[:16]:16s} | {e.get('schedule','')}{mark}")
        if len(future) > 80:
            print(f"  ... and {len(future) - 80} more")
        return

    clean = [{k: v for k, v in e.items() if k != "protected"} for e in future]
    out = {
        "version": 1,
        "scraped_at": datetime.now().astimezone().isoformat(),
        "horizon_days": args.horizon_days,
        "event_count": len(clean),
        "listings": clean,
    }
    p = Path(args.output)
    p.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n  Written: {p} ({p.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
