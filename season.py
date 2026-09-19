"""
Season management: start/sit, waiver/free-agent, and trade recommendations
for an ongoing fantasy season, built on top of saved league rosters and
uploaded player projections.

This is deliberately a separate, decoupled module from engine.py (the
draft-day tool) - a season roster isn't "drafted from a pool", it's just
a saved list of player names per team, and the recommendation questions
here (who should start, who's worth picking up, who to target in a
trade) are a different problem from draft-day pick recommendations.

Persistence: simple JSON files on disk, no database. Three files:
  - league.json: the shared league itself - team names/rosters, num
    teams, starter requirements. Shared across everyone using this
    deployment, the same way the FFC ADP cache is shared - a real
    league is one shared reality, not something each visitor should
    have to re-enter separately. Each visitor just points at which
    team number is theirs (stored per-session in Flask's session, not
    here - see app.py).
  - projections.json: uploaded player projections, keyed by week (an
    integer) or "season" for season-long/rest-of-season projections.
    Also shared - one set of projections for everyone using this
    league. Uploading for a given week replaces that week's data.
  - player_ids.json: a learned name<->stable-ID map (see "Player
    matching" below).

The player universe for waiver/free-agent purposes is derived from
projections themselves (whoever has a projection is a "known" player) -
there's no separate player-list upload. Free agents = players with a
projection who aren't on any saved roster.

Player matching
----------------
Rosters are typed in by hand as plain names; projections come from
wherever the user's projections pipeline exports them (e.g. an
nflverse-based one). Matching those two by exact name string is
fragile - suffixes, initials, and formatting drift ("Patrick Mahomes"
vs "P. Mahomes") silently break matching with no error, just a missing
projection.

If an upload includes a stable player ID (nflverse's gsis_id, or any
other consistent ID - see normalize_projection_records), this module
learns the name<->ID association and keeps it in player_ids.json.
Every lookup afterward tries an exact name match first, and only falls
back to the learned ID if that fails - so a player who was matched by
name once stays matchable later even if a subsequent upload spells
their name differently, as long as the ID is consistent. Without any
ID in the data, everything still works exactly as before (exact-name
matching only).
"""
import json
import os
import threading
from datetime import datetime, timezone

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "season_data")
LEAGUE_PATH = os.path.join(DATA_DIR, "league.json")
PROJECTIONS_PATH = os.path.join(DATA_DIR, "projections.json")
ID_MAP_PATH = os.path.join(DATA_DIR, "player_ids.json")

_lock = threading.Lock()

DEFAULT_STARTERS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "DEF": 1, "K": 1}
FLEX_ELIGIBLE_POSITIONS = {"RB", "WR", "TE"}


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _default_league(num_teams=12):
    return {
        "num_teams": num_teams,
        "starters": dict(DEFAULT_STARTERS),
        "teams": {
            str(i): {"name": f"Team {i}", "roster": []}
            for i in range(1, num_teams + 1)
        },
    }


def load_league():
    _ensure_data_dir()
    if not os.path.exists(LEAGUE_PATH):
        return _default_league()
    with open(LEAGUE_PATH) as f:
        return json.load(f)


def save_league(league):
    _ensure_data_dir()
    with _lock:
        with open(LEAGUE_PATH, "w") as f:
            json.dump(league, f, indent=2)


def update_league_settings(num_teams=None, starters=None):
    league = load_league()
    if num_teams is not None and num_teams != league["num_teams"]:
        old_teams = league["teams"]
        new_teams = {}
        for i in range(1, num_teams + 1):
            key = str(i)
            new_teams[key] = old_teams.get(key, {"name": f"Team {i}", "roster": []})
        league["teams"] = new_teams
        league["num_teams"] = num_teams
    if starters is not None:
        league["starters"] = starters
    save_league(league)
    return league


def update_team_roster(team_number, name=None, roster=None):
    league = load_league()
    key = str(team_number)
    if key not in league["teams"]:
        raise ValueError(f"Team {team_number} doesn't exist in a {league['num_teams']}-team league.")
    if name is not None:
        league["teams"][key]["name"] = name
    if roster is not None:
        league["teams"][key]["roster"] = roster
    save_league(league)
    return league["teams"][key]


def all_rostered_players(league=None):
    """Every player name rostered on any team, across the whole league."""
    league = league or load_league()
    rostered = set()
    for team in league["teams"].values():
        rostered.update(team.get("roster", []))
    return rostered


# ----------------------------------------------------------------------
# Player ID matching
# ----------------------------------------------------------------------

def _name_key(name):
    return name.strip().lower()


def _load_id_map():
    _ensure_data_dir()
    if not os.path.exists(ID_MAP_PATH):
        return {"name_to_id": {}, "id_to_name": {}}
    with open(ID_MAP_PATH) as f:
        return json.load(f)


def _save_id_map(id_map):
    _ensure_data_dir()
    with _lock:
        with open(ID_MAP_PATH, "w") as f:
            json.dump(id_map, f, indent=2)


def _learn_player_ids(records):
    """Learns name<->stable-ID associations from any records that carry
    both (e.g. an nflverse gsis_id), so the same player can still be
    matched later even if a later upload spells their name slightly
    differently."""
    id_map = _load_id_map()
    changed = False
    for r in records:
        pid = r.get("player_id")
        name = r.get("name")
        if not pid or not name:
            continue
        key = _name_key(name)
        if id_map["name_to_id"].get(key) != pid:
            id_map["name_to_id"][key] = pid
            changed = True
        if id_map["id_to_name"].get(pid) != name:
            id_map["id_to_name"][pid] = name
            changed = True
    if changed:
        _save_id_map(id_map)


def resolve_player_id(name):
    """The stable player ID associated with this name, if any upload has
    ever paired the two - None if this exact name string has never been
    seen alongside an ID."""
    return _load_id_map()["name_to_id"].get(_name_key(name))


def known_player_id_count():
    """How many distinct players currently have a learned stable ID -
    surfaced in the UI so it's visible whether ID-based matching is
    actually active for this league's data."""
    return len(_load_id_map()["id_to_name"])


# ----------------------------------------------------------------------
# Projections
# ----------------------------------------------------------------------

def load_projections():
    _ensure_data_dir()
    if not os.path.exists(PROJECTIONS_PATH):
        return {}
    with open(PROJECTIONS_PATH) as f:
        return json.load(f)


def save_projections_for_week(week_key, records, updated_at=None):
    """week_key: an int week number, or the string 'season' for season-
    long/rest-of-season projections. Replaces whatever was previously
    stored for that week."""
    _ensure_data_dir()
    with _lock:
        data = load_projections()
        data[str(week_key)] = {
            "updated_at": updated_at or datetime.now(timezone.utc).isoformat(),
            "players": records,
        }
        with open(PROJECTIONS_PATH, "w") as f:
            json.dump(data, f, indent=2)
    _learn_player_ids(records)


def _week_entry(week_key):
    """Whichever stored projection set applies: the requested week,
    falling back to season-long, then to any week at all so waiver/
    trade tools still work before a specific week's projections are
    uploaded."""
    data = load_projections()
    entry = None
    if week_key is not None:
        entry = data.get(str(week_key))
    if not entry:
        entry = data.get("season")
    if not entry and data:
        entry = next(iter(data.values()))
    return entry


def _build_player_index(week_key):
    """This week's projections indexed by both name and stable ID, so a
    lookup can fall back to ID matching when an exact name match
    fails."""
    entry = _week_entry(week_key)
    if not entry:
        return {"by_name": {}, "by_id": {}}
    by_name = {p["name"]: p for p in entry["players"]}
    by_id = {p["player_id"]: p for p in entry["players"] if p.get("player_id")}
    return {"by_name": by_name, "by_id": by_id}


def _lookup_player(name, index):
    """A roster/free-agent name's projection record: exact name match
    first (correct whenever spelling lines up), falling back to a
    stable ID learned from any previous upload where this name and an
    ID appeared together - so a player doesn't drop out of matching
    just because this week's source formats their name differently
    than an earlier one did. Returns None if neither resolves."""
    rec = index["by_name"].get(name)
    if rec:
        return rec
    pid = resolve_player_id(name)
    if pid:
        return index["by_id"].get(pid)
    return None


def normalize_projection_records(df):
    """Turns an uploaded CSV (as a pandas DataFrame) into the plain
    records this module stores. Column names are matched loosely (case/
    spacing-insensitive), mirroring how the draft CSV importer handles
    player/name and pos/position aliases. A stable player ID column is
    optional but recommended - see the module docstring - and is
    matched against common nflverse/ffverse naming conventions
    (gsis_id being the standard nflverse one)."""
    cols = {c.strip().lower().replace(" ", "_"): c for c in df.columns}

    def col(*names):
        for n in names:
            if n in cols:
                return cols[n]
        return None

    name_col = col("name", "player", "player_name")
    pos_col = col("position", "pos")
    team_col = col("team", "tm")
    proj_col = col("proj_points", "projection", "points", "proj", "fantasy_points", "fpts")
    id_col = col("player_id", "gsis_id", "nflverse_id", "nfl_id", "gsis")

    if not name_col:
        raise ValueError("No player-name column found (expected 'name', 'player', or 'player_name').")
    if not proj_col:
        raise ValueError("No projection column found (expected 'proj_points', 'points', 'proj', or 'fpts').")

    records = []
    for _, row in df.iterrows():
        name = str(row[name_col]).strip()
        if not name or name.lower() == "nan":
            continue
        try:
            proj_points = float(row[proj_col])
        except (TypeError, ValueError):
            continue
        player_id = None
        if id_col:
            raw_id = row[id_col]
            if raw_id is not None and str(raw_id).strip() and str(raw_id).strip().lower() != "nan":
                player_id = str(raw_id).strip()
        records.append({
            "name": name,
            "position": str(row[pos_col]).upper().strip() if pos_col and str(row[pos_col]).strip() else None,
            "team": str(row[team_col]).strip() if team_col and str(row[team_col]).strip() else None,
            "proj_points": proj_points,
            "player_id": player_id,
        })
    if not records:
        raise ValueError("No usable rows found - check that the projection column has numeric values.")
    return records


# ----------------------------------------------------------------------
# Start / Sit
# ----------------------------------------------------------------------

def _optimal_lineup(roster, starters_req, index):
    """Given a roster (list of names) and a player index (see
    _build_player_index), fills each dedicated starting slot and any
    FLEX slots with whichever eligible players have the highest
    projection, and totals it up. This is the core "what's the best
    lineup this roster can field" calculation - used directly by
    Start/Sit, and reused by trade evaluation to score a roster both
    before and after a hypothetical swap. Missing projections count as
    0 toward the total (there's no better number to use without data),
    consistent with how the lineup filler already tolerates missing
    projections elsewhere."""
    players = []
    for name in roster:
        rec = _lookup_player(name, index) or {}
        players.append({
            "name": name,
            "position": rec.get("position") or "UNK",
            "team": rec.get("team"),
            "proj": rec.get("proj_points"),
        })

    # Fill dedicated slots first (best projection at each position),
    # then FLEX from whatever RB/WR/TE is left over - so a bench player
    # doesn't get "start" advice for a slot their position can't fill,
    # and the single best remaining flex-eligible player (regardless of
    # which of RB/WR/TE) gets the flex slot rather than double-counting
    # someone already placed in a dedicated slot.
    remaining = sorted(players, key=lambda p: (p["proj"] is None, -(p["proj"] or 0)))
    lineup = []

    for pos, count in starters_req.items():
        if pos == "FLEX":
            continue
        eligible = [p for p in remaining if p["position"] == pos]
        for p in eligible[:count]:
            lineup.append({**p, "slot": pos})
            remaining.remove(p)

    flex_count = starters_req.get("FLEX", 0)
    flex_eligible = [p for p in remaining if p["position"] in FLEX_ELIGIBLE_POSITIONS]
    for p in flex_eligible[:flex_count]:
        lineup.append({**p, "slot": "FLEX"})
        remaining.remove(p)

    bench = remaining
    total = round(sum(p["proj"] or 0 for p in lineup), 1)
    return {"lineup": lineup, "bench": bench, "total": total}


def get_start_sit(team_number, week_key):
    """Optimal lineup for this team this week - not whoever happens to
    be currently "starting" (there's no such saved state; this always
    recomputes the best lineup fresh from the roster + projections)."""
    league = load_league()
    key = str(team_number)
    if key not in league["teams"]:
        raise ValueError(f"Team {team_number} doesn't exist in a {league['num_teams']}-team league.")

    roster = league["teams"][key]["roster"]
    index = _build_player_index(week_key)
    result = _optimal_lineup(roster, league["starters"], index)
    missing_projections = [p["name"] for p in result["lineup"] if p["proj"] is None]

    return {
        "team": league["teams"][key]["name"],
        "week": week_key,
        "lineup": result["lineup"],
        "bench": result["bench"],
        "total": result["total"],
        "warning": (
            f"No projection found for: {', '.join(missing_projections)} - "
            "they're placed by roster order, not projection, until projections are uploaded for them."
        ) if missing_projections else None,
    }


# ----------------------------------------------------------------------
# Waiver / free-agent recommendations
# ----------------------------------------------------------------------

def _rostered_names_and_ids(league):
    """Everyone rostered anywhere, by both name and (where known) stable
    ID - so a free-agent search can exclude a player correctly even if
    their roster entry and their projections entry spell their name
    differently."""
    names, ids = set(), set()
    for team in league["teams"].values():
        for name in team.get("roster", []):
            names.add(name)
            pid = resolve_player_id(name)
            if pid:
                ids.add(pid)
    return names, ids


def get_waiver_recommendations(team_number, week_key, min_improvement=3.0):
    """For each bench player on this team, find the best available
    (unrostered anywhere in the league) free agent at the same position
    who projects meaningfully higher this week. A simple, transparent
    "upgrade" heuristic - not a full add/drop optimizer."""
    league = load_league()
    key = str(team_number)
    if key not in league["teams"]:
        raise ValueError(f"Team {team_number} doesn't exist in a {league['num_teams']}-team league.")

    roster = league["teams"][key]["roster"]
    rostered_names, rostered_ids = _rostered_names_and_ids(league)
    index = _build_player_index(week_key)

    if not index["by_name"]:
        return {"recommendations": [], "warning": f"No projections uploaded for week {week_key} yet."}

    free_agents_by_pos = {}
    for name, rec in index["by_name"].items():
        if name in rostered_names:
            continue
        pid = rec.get("player_id")
        if pid and pid in rostered_ids:
            continue  # same player, just rostered under a differently-spelled name
        pos = rec.get("position")
        if not pos:
            continue
        free_agents_by_pos.setdefault(pos, []).append({
            "name": name, "position": pos, "team": rec.get("team"),
            "proj": rec.get("proj_points"),
        })
    for pos in free_agents_by_pos:
        free_agents_by_pos[pos].sort(key=lambda p: -(p["proj"] if p["proj"] is not None else -999))

    recommendations = []
    for name in roster:
        rec = _lookup_player(name, index)
        if not rec:
            continue
        pos = rec.get("position")
        my_proj = rec.get("proj_points")
        if not pos or my_proj is None:
            continue
        candidates = free_agents_by_pos.get(pos, [])
        for fa in candidates[:3]:
            if fa["proj"] is None:
                continue
            improvement = fa["proj"] - my_proj
            if improvement >= min_improvement:
                recommendations.append({
                    "drop": name,
                    "drop_proj": my_proj,
                    "add": fa["name"],
                    "add_proj": fa["proj"],
                    "position": pos,
                    "improvement": round(improvement, 1),
                })

    recommendations.sort(key=lambda r: -r["improvement"])
    return {"recommendations": recommendations, "warning": None}


# ----------------------------------------------------------------------
# Trade suggestions
# ----------------------------------------------------------------------

def _position_surplus_need(team, week_key, league):
    """Positive = surplus (more depth than starters need at that
    position), negative = need. Uses season-long projections when
    available (rest-of-season value matters more for trades than one
    week's noise), falling back to whatever week was requested."""
    index = _build_player_index(week_key)
    starters_req = league["starters"]
    counts = {}
    for name in team["roster"]:
        rec = _lookup_player(name, index)
        pos = rec.get("position") if rec else None
        if pos:
            counts[pos] = counts.get(pos, 0) + 1

    result = {}
    for pos, required in starters_req.items():
        if pos == "FLEX":
            continue
        result[pos] = counts.get(pos, 0) - required

    # FLEX is a real, separate roster slot - not a position a player
    # can BE, but a shared bucket that RB/WR/TE surplus fills. This
    # used to be skipped entirely, so a team could have every dedicated
    # RB/WR/TE slot exactly filled and nothing more - leaving FLEX
    # permanently empty - with nothing here ever flagging that as a
    # need. Flex balance = however much RB/WR/TE surplus exists beyond
    # each position's own dedicated requirement, combined, minus however
    # many FLEX slots need filling from that pool.
    flex_required = starters_req.get("FLEX", 0)
    if flex_required > 0:
        flex_eligible_surplus = sum(
            max(0, result.get(pos, 0)) for pos in FLEX_ELIGIBLE_POSITIONS
        )
        result["FLEX"] = flex_eligible_surplus - flex_required
    return result


def _team_players_with_projection(team, week_key):
    """{name, position, team, proj} for every rostered player this
    module can identify a position for (via the projection index - a
    player with no projection uploaded anywhere, under any name/ID this
    module has seen, can't be positioned or valued yet)."""
    index = _build_player_index(week_key)
    players = []
    for name in team["roster"]:
        rec = _lookup_player(name, index)
        if not rec or not rec.get("position"):
            continue
        players.append({
            "name": name,
            "position": rec["position"],
            "team": rec.get("team"),
            "proj": rec.get("proj_points"),
        })
    return players


def _surplus_players_by_position(team, league, week_key):
    """The actual players making up each position's surplus - i.e. the
    ones beyond however many dedicated starting slots that position
    has, ranked by projection (best-of-the-overflow first). These are
    the players actually worth offering in a trade: a team's starters
    at a position aren't "surplus" just because the position has depth,
    only the bodies beyond what's needed to start are. Positions with
    no surplus are omitted.

    Also computes a "FLEX" bucket: whichever RB/WR/TE overflow is left
    over after covering the shared FLEX slot(s) - genuinely excess
    depth with no roster purpose at all, and the actual trade chips
    behind a "FLEX surplus" (see _position_surplus_need). This used to
    not exist, so FLEX-level depth had no players attached to it for
    trade matching to actually use."""
    players = _team_players_with_projection(team, week_key)
    starters_req = league["starters"]
    surplus = {}
    flex_pool = []
    for pos, required in starters_req.items():
        if pos == "FLEX":
            continue
        pos_players = sorted(
            [p for p in players if p["position"] == pos],
            key=lambda p: (p["proj"] is None, -(p["proj"] or 0)),
        )
        if len(pos_players) > required:
            overflow = pos_players[required:]
            surplus[pos] = overflow
            if pos in FLEX_ELIGIBLE_POSITIONS:
                flex_pool.extend(overflow)

    flex_required = starters_req.get("FLEX", 0)
    if flex_required > 0 and len(flex_pool) > flex_required:
        flex_pool_sorted = sorted(flex_pool, key=lambda p: (p["proj"] is None, -(p["proj"] or 0)))
        surplus["FLEX"] = flex_pool_sorted[flex_required:]
    return surplus


def _match_trade_pairs(their_offers, my_offers, max_pairs=3):
    """Greedily pairs players from the two offer pools by closest
    rest-of-season projection value, so a suggested exchange is at
    least plausibly fair rather than pairing a league-winner with a
    replacement-level player just because both happen to be "surplus".
    Only players with a real projection can be matched - value-matching
    a number against nothing isn't meaningful."""
    pairs = []
    used_mine = set()
    for their_player in sorted(their_offers, key=lambda p: -(p["proj"] or -1)):
        if their_player["proj"] is None:
            continue
        best_match, best_diff = None, None
        for mine in my_offers:
            if mine["name"] in used_mine or mine["proj"] is None:
                continue
            diff = abs(mine["proj"] - their_player["proj"])
            if best_diff is None or diff < best_diff:
                best_match, best_diff = mine, diff
        if best_match:
            used_mine.add(best_match["name"])
            pairs.append({
                "you_send": best_match["name"],
                "you_send_position": best_match["position"],
                "you_send_proj": best_match["proj"],
                "you_get": their_player["name"],
                "you_get_position": their_player["position"],
                "you_get_proj": their_player["proj"],
            })
    pairs.sort(key=lambda p: abs(p["you_send_proj"] - p["you_get_proj"]))
    return pairs[:max_pairs]


def _simulate_trade_impact(my_roster, their_roster, starters_req, index, send_name, get_name):
    """The actual payoff question for a trade: not just "are these two
    players roughly equal value" (that's _match_trade_pairs, which only
    filters for plausibility), but "does swapping them actually make
    either team's best lineup better". Recomputes each side's optimal
    lineup total (see _optimal_lineup - the same calculation Start/Sit
    uses) both with the current roster and with the trade applied, and
    returns the before/after/delta for both teams."""
    my_before = _optimal_lineup(my_roster, starters_req, index)["total"]
    their_before = _optimal_lineup(their_roster, starters_req, index)["total"]

    my_after_roster = [p for p in my_roster if p != send_name] + [get_name]
    their_after_roster = [p for p in their_roster if p != get_name] + [send_name]
    my_after = _optimal_lineup(my_after_roster, starters_req, index)["total"]
    their_after = _optimal_lineup(their_after_roster, starters_req, index)["total"]

    return {
        "your_lineup_before": my_before,
        "your_lineup_after": my_after,
        "your_lineup_delta": round(my_after - my_before, 1),
        "opponent_lineup_before": their_before,
        "opponent_lineup_after": their_after,
        "opponent_lineup_delta": round(their_after - their_before, 1),
    }


def get_trade_suggestions(team_number, week_key="season"):
    """For each other team in the league:
    1. Identify my positional needs and surpluses.
    2. Identify their positional needs and surpluses.
    3. Find where our situations are opposite (they're deep where I'm
       thin, or vice versa).
    4. Pull the actual overflow players behind each side of that
       mismatch - not just "position X", specific rostered names.
    5. Use rest-of-season projections to match those players into
       plausible, value-comparable exchanges.
    6. Score each candidate pair by actual roster impact - each team's
       optimal starting lineup total, before vs. after the swap - not
       just whether the two players are similarly valued.

    Concrete player-for-player pairs only get generated when the fit
    is mutual (they have a surplus I need AND I have a surplus they
    need) - a one-sided "give me your good player" isn't a plausible
    trade to propose. Pairs that don't actually improve YOUR lineup are
    dropped - if a swap doesn't help you, it isn't a recommendation,
    whatever the raw player values look like. One-sided position
    matches with no viable pair are still surfaced (as they_could_offer
    with no pairs) so you can see partial fits too, but this is a
    starting point for a conversation, not a value-balanced trade
    calculator or a guarantee either side would actually agree to it."""
    league = load_league()
    key = str(team_number)
    if key not in league["teams"]:
        raise ValueError(f"Team {team_number} doesn't exist in a {league['num_teams']}-team league.")

    my_team = league["teams"][key]
    my_needs = _position_surplus_need(my_team, week_key, league)
    my_need_positions = {pos for pos, val in my_needs.items() if val < 0}
    my_surplus = _surplus_players_by_position(my_team, league, week_key)
    index = _build_player_index(week_key)

    suggestions = []
    for other_key, other_team in league["teams"].items():
        if other_key == key:
            continue
        other_needs = _position_surplus_need(other_team, week_key, league)
        other_need_positions = {pos for pos, val in other_needs.items() if val < 0}
        other_surplus = _surplus_players_by_position(other_team, league, week_key)

        they_have_what_i_need = my_need_positions & set(other_surplus.keys())
        i_have_what_they_need = set(my_surplus.keys()) & other_need_positions
        if not they_have_what_i_need:
            continue

        pairs = []
        if i_have_what_they_need:
            their_offers = [p for pos in they_have_what_i_need for p in other_surplus[pos]]
            my_offers = [p for pos in i_have_what_they_need for p in my_surplus[pos]]
            candidate_pairs = _match_trade_pairs(their_offers, my_offers)

            for pair in candidate_pairs:
                impact = _simulate_trade_impact(
                    my_team["roster"], other_team["roster"], league["starters"], index,
                    send_name=pair["you_send"], get_name=pair["you_get"],
                )
                if impact["your_lineup_delta"] <= 0:
                    continue  # doesn't actually help your lineup - not a real recommendation
                # A "surplus" player is by construction each team's
                # weakest at that position, so a value-matched swap
                # doesn't automatically lift both sides' lineups - e.g.
                # giving up a player who was productively filling a
                # FLEX spot for a new starter who projects lower nets a
                # real loss for that side even at "similar" value. Don't
                # hide those pairs (you may still want to see and
                # propose them), but flag genuine win-wins - where the
                # opponent's own lineup math also improves, so there's
                # a real chance they'd say yes - so the better trades
                # stand out.
                pairs.append({
                    **pair, **impact,
                    "mutually_beneficial": impact["opponent_lineup_delta"] > 0,
                })

            # Mutually beneficial trades sort first, even when a non-
            # mutual pair has a bigger raw delta for you - a trade
            # someone would actually accept is worth more than a
            # bigger number they'd likely reject.
            pairs.sort(key=lambda p: (-p["mutually_beneficial"], -p["your_lineup_delta"]))

        suggestions.append({
            "team": other_team["name"],
            "they_could_offer": sorted(they_have_what_i_need),
            "mutual_fit": sorted(i_have_what_they_need) if i_have_what_they_need else None,
            "pairs": pairs,
        })

    def _suggestion_sort_key(s):
        if not s["pairs"]:
            return (1, 999)  # no pairs at all sorts last
        top = s["pairs"][0]  # already sorted mutually-beneficial-first
        return (0 if top["mutually_beneficial"] else 1, -top["your_lineup_delta"])

    suggestions.sort(key=_suggestion_sort_key)
    return {"suggestions": suggestions, "my_needs": my_needs}
