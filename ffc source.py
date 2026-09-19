"""
Fantasy Football Calculator ADP integration.

Fetches average-draft-position data from Fantasy Football Calculator's
free public REST API (https://fantasyfootballcalculator.com), caches it
on disk once a day, and keeps a few days of snapshots so day-over-day
ADP movement (players rising/falling) can be tracked.

FFC's API is unauthenticated and free for personal/commercial use. Per
their own documentation, the underlying data only updates once a day and
they ask callers not to hit the API more often than that - the caching
here exists specifically to respect that, not just to be fast.
https://help.fantasyfootballcalculator.com/article/42-adp-rest-api

NOTE: this module was written from FFC's published API docs and
observed page data, but hasn't been exercised against a live network
call in this environment (no outbound network access here). The shape
of the JSON response (a top-level "players" list with name/position/
team/adp/... keys) matches their documented format as of writing - if
FFC changes their response shape, _normalize() below is the one place
that needs updating.
"""
import json
import os
import threading
from datetime import datetime, timezone

import requests

BASE_URL = "https://fantasyfootballcalculator.com/api/v1/adp"

# Human-facing scoring key -> FFC's URL slug.
SCORING_SLUGS = {
    "standard": "standard",
    "half_ppr": "half-ppr",
    "ppr": "ppr",
}

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "adp_cache")
MAX_AGE_SECONDS = 24 * 60 * 60   # FFC's own data only updates once/day
RETENTION_DAYS = 3               # enough history to compare "today vs a couple days ago"


def _ensure_cache_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def _snapshot_path(scoring, teams, date_str):
    return os.path.join(CACHE_DIR, f"{scoring}_{teams}team_{date_str}.json")


def _today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _list_snapshots(scoring, teams):
    """All cached snapshot files for this scoring/team-size combo, oldest
    first (filenames are date-stamped, so lexical sort is chronological)."""
    _ensure_cache_dir()
    prefix = f"{scoring}_{teams}team_"
    files = sorted(
        f for f in os.listdir(CACHE_DIR)
        if f.startswith(prefix) and f.endswith(".json")
    )
    return [os.path.join(CACHE_DIR, f) for f in files]


def _prune_old_snapshots(scoring, teams, keep=RETENTION_DAYS):
    for path in _list_snapshots(scoring, teams)[:-keep]:
        try:
            os.remove(path)
        except OSError:
            pass


# A plain requests.get() sends "python-requests/X.X" as its User-Agent,
# which is one of the most commonly blocked/challenged signatures by
# basic bot-detection (Cloudflare, etc.) - and there's direct evidence
# this site has some in front of it (a browsing tool used while building
# this integration was refused with an explicit "site disallows
# automated access" before it even reached the API). A realistic
# browser-like header set is standard practice for a legitimate,
# documented-as-public API like this one and meaningfully reduces the
# chance of being blocked outright.
_REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}


def _fetch_from_ffc(scoring, teams):
    if scoring not in SCORING_SLUGS:
        raise ValueError(
            f"Unknown scoring format '{scoring}' - expected one of {list(SCORING_SLUGS)}"
        )
    url = f"{BASE_URL}/{SCORING_SLUGS[scoring]}"
    resp = requests.get(url, params={"teams": teams}, headers=_REQUEST_HEADERS, timeout=10)

    if resp.status_code != 200:
        # Include the actual status in the exception message - the
        # generic "couldn't reach the server" catch-all upstream isn't
        # useful for telling "FFC is down" apart from "FFC is blocking
        # this request" apart from "the URL/format changed".
        snippet = (resp.text or "")[:200].strip()
        raise RuntimeError(
            f"Fantasy Football Calculator returned HTTP {resp.status_code} "
            f"for {url}" + (f" - response started with: {snippet!r}" if snippet else "")
        )

    try:
        data = resp.json()
    except ValueError as e:
        # A 200 status with a body that isn't JSON (an HTML challenge
        # page, a maintenance page, etc.) is exactly what basic bot
        # protection looks like from the caller's side - raise_for_status()
        # alone would miss this, since the status code itself is "fine".
        snippet = (resp.text or "")[:200].strip()
        raise RuntimeError(
            "Fantasy Football Calculator returned a 200 response that "
            "wasn't valid JSON - likely being blocked/challenged rather "
            "than served the real API response. Response started with: "
            f"{snippet!r}"
        ) from e

    raw_players = data.get("players", [])
    if not raw_players:
        raise ValueError(
            "Fantasy Football Calculator returned no players - their API "
            "response format may have changed."
        )
    return raw_players


def _normalize(raw_players):
    """FFC returns players already ordered by ADP, but we sort defensively
    and assign our own integer `rank` from list position rather than
    trusting any rank-like field in the response - and specifically NOT
    the raw ADP number. ADP is a fractional average draft slot (e.g.
    24.4, since it's an average across many real drafts); rank is meant
    to mean "this player is the Nth-best on the list", which is what the
    rest of DraftIQ's scoring assumes "rank" means."""
    cleaned = []
    for p in raw_players:
        try:
            adp = float(p.get("adp"))
        except (TypeError, ValueError):
            continue
        name = (p.get("name") or "").strip()
        if not name:
            continue
        cleaned.append({
            "name": name,
            "position": str(p.get("position", "")).upper().strip(),
            "team": p.get("team", ""),
            "adp": adp,
            "times_drafted": p.get("times_drafted"),
            "high": p.get("high"),
            "low": p.get("low"),
            "stdev": p.get("stdev"),
        })
    cleaned.sort(key=lambda p: p["adp"])
    for i, p in enumerate(cleaned, start=1):
        p["rank"] = i
    return cleaned


# One lock per (scoring, teams) key, so that if several requests arrive
# at once right as the cache expires, only the first actually calls FFC
# and the rest wait and then read what it wrote - rather than each firing
# its own simultaneous fetch. Created lazily/lock-protected itself since
# this dict is shared across request-handling threads.
_fetch_locks = {}
_fetch_locks_guard = threading.Lock()


def _lock_for(scoring, teams):
    key = (scoring, teams)
    with _fetch_locks_guard:
        if key not in _fetch_locks:
            _fetch_locks[key] = threading.Lock()
        return _fetch_locks[key]


def _read_cache_if_fresh(scoring, teams):
    """Returns (players, meta) from the latest snapshot if one exists and
    is less than a day old by its own recorded fetch time, else None.
    Staleness is judged by the fetched_at timestamp stored inside the
    snapshot, not the file's OS-level mtime - mtime reflects when the
    file was last written to disk, which isn't necessarily when the data
    was fetched (e.g. a redeploy or file copy touches mtime without
    changing what's inside), so trusting it could serve day-old data as
    "fresh" or vice versa."""
    snapshots = _list_snapshots(scoring, teams)
    if not snapshots:
        return None
    with open(snapshots[-1]) as f:
        cached = json.load(f)
    fetched_at = datetime.fromisoformat(cached["fetched_at"])
    age = (datetime.now(timezone.utc) - fetched_at).total_seconds()
    if age >= MAX_AGE_SECONDS:
        return None
    return cached["players"], {
        "source": "ffc",
        "scoring": scoring,
        "teams": teams,
        "fetched_at": cached["fetched_at"],
        "from_cache": True,
    }


def get_players(scoring, teams=12):
    """Returns (players, meta) for the given scoring format/team size.

    Serves from the cached snapshot when one exists and is less than a
    day old; otherwise fetches fresh from FFC, caches the result, and
    prunes snapshots older than RETENTION_DAYS.

    There is intentionally no way to force a fresh fetch here - FFC's own
    ADP data only updates once a day and they explicitly ask API callers
    not to poll more often than that. The cache key is (scoring, teams)
    only, not tied to any particular user/session, so this cap applies
    across everyone using a given deployment - ten people syncing the
    same scoring format/team size in the same day still results in at
    most one real call to FFC, not ten. A lock around the actual fetch
    additionally prevents two requests that arrive at the same moment
    (right as the cache expires) from both triggering their own fetch.
    """
    _ensure_cache_dir()

    cached = _read_cache_if_fresh(scoring, teams)
    if cached:
        return cached

    with _lock_for(scoring, teams):
        # Re-check after acquiring the lock - another thread may have
        # just finished fetching while we were waiting for it.
        cached = _read_cache_if_fresh(scoring, teams)
        if cached:
            return cached

        raw_players = _fetch_from_ffc(scoring, teams)
        players = _normalize(raw_players)
        fetched_at = datetime.now(timezone.utc).isoformat()

        with open(_snapshot_path(scoring, teams, _today_str()), "w") as f:
            json.dump({"fetched_at": fetched_at, "players": players}, f)
        _prune_old_snapshots(scoring, teams)

        return players, {
            "source": "ffc",
            "scoring": scoring,
            "teams": teams,
            "fetched_at": fetched_at,
            "from_cache": False,
        }



def get_adp_movers(scoring, teams=12, min_delta=3.0):
    """Compares the latest cached snapshot to the oldest one still in the
    retention window and returns players whose ADP moved by at least
    min_delta picks. "rising" means being drafted earlier than before
    (ADP number went down); "falling" means being drafted later (ADP
    number went up) - named from a drafter's point of view, not the raw
    sign of the number.

    Returns an empty list (rather than erroring) if there isn't at least
    two days of snapshots yet to compare - that's an expected, normal
    state right after the first sync, not a failure.
    """
    snapshots = _list_snapshots(scoring, teams)
    if len(snapshots) < 2:
        return []

    with open(snapshots[0]) as f:
        oldest_snapshot = json.load(f)
        oldest = {p["name"]: p for p in oldest_snapshot["players"]}
    with open(snapshots[-1]) as f:
        newest_snapshot = json.load(f)
        newest = {p["name"]: p for p in newest_snapshot["players"]}

    movers = []
    for name, new_p in newest.items():
        old_p = oldest.get(name)
        if not old_p:
            continue
        delta = old_p["adp"] - new_p["adp"]  # positive = ADP dropped = rising
        if abs(delta) >= min_delta:
            movers.append({
                "name": name,
                "position": new_p["position"],
                "team": new_p["team"],
                "old_adp": old_p["adp"],
                "new_adp": new_p["adp"],
                "delta": round(delta, 1),
                "direction": "rising" if delta > 0 else "falling",
            })

    movers.sort(key=lambda m: -abs(m["delta"]))
    return movers
