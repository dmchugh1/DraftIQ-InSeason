import math
import os
import threading
import time
import uuid

from flask import Flask, jsonify, render_template, request, session
import numpy as np
import pandas as pd
import requests

from engine import DraftIQEngine
import ffc_source
import season

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8MB upload cap

# Needed for Flask's signed session cookie (see multi-user section below).
# Pin SECRET_KEY in your environment if you want cookies to survive a
# server restart - not required for correctness (a restart already loses
# all in-memory draft state regardless, so a new cookie just means a new,
# empty draft, same as any other cold start), but pinning it means a
# returning visitor keeps their draft across a restart that happens to
# not evict their entry, and avoids invalidating everyone's session at
# once on every redeploy.
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(24)

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

_sample_path = os.path.join(UPLOAD_DIR, "sample_players.csv")

# ----------------------------------------------------------------------
# Multi-user support
# ----------------------------------------------------------------------
# Each visitor's browser gets its own signed session cookie (Flask's
# `session`, not tied to a login - no account needed) identifying them,
# and each session id maps to its own DraftIQEngine instance below. Two
# different people using the same deployment therefore get fully
# separate draft states instead of silently sharing and corrupting one
# global draft, which is what a single module-level `engine = ...`
# instance would do the moment a second visitor showed up.
#
# This is in-memory only - no login, no database. That's a deliberate
# scope choice for a personal/league tool: it's enough for a group of
# people to each run their own draft against the same deployment, but it
# means draft state doesn't survive a server restart, and this design
# only works correctly behind a single server process (a second process
# - e.g. most "auto-scaling" hosting - would have its own separate
# in-memory dict, splitting one visitor's requests across two different
# stores depending on which process handled them). Fine for Render's
# free/starter single-instance tier; would need a real shared store
# (Redis, a database) before scaling to multiple processes.
#
# The Fantasy Football Calculator ADP cache in ffc_source.py is
# deliberately NOT part of this per-session state - it stays a single
# shared, file-based cache keyed only by scoring format/team size, which
# is what keeps everyone on a shared deployment drawing from the same
# once-a-day fetch instead of each session triggering its own.
_engines = {}
_engines_last_used = {}
_engines_lock = threading.Lock()
ENGINE_IDLE_EXPIRY_SECONDS = 6 * 60 * 60  # evict sessions idle 6+ hours


def _new_engine():
    eng = DraftIQEngine()
    # Auto-load the bundled sample dataset if present, so a brand-new
    # session has something to show immediately. Real usage: upload a
    # players.csv or sync from Fantasy Football Calculator from Setup.
    if os.path.exists(_sample_path):
        eng.load_players(_sample_path)
    return eng


def get_engine():
    """Returns this visitor's DraftIQEngine, creating both their session
    id and a fresh engine on first visit. Also lazily evicts engines for
    sessions that have been idle past ENGINE_IDLE_EXPIRY_SECONDS - run
    here rather than on a background thread/timer, which is simpler and
    entirely sufficient at personal/league scale (a handful to a few
    dozen concurrent users, not thousands)."""
    if "user_id" not in session:
        session["user_id"] = str(uuid.uuid4())
        session.permanent = True
    user_id = session["user_id"]

    with _engines_lock:
        now = time.time()
        expired = [
            uid for uid, last in _engines_last_used.items()
            if now - last > ENGINE_IDLE_EXPIRY_SECONDS
        ]
        for uid in expired:
            _engines.pop(uid, None)
            _engines_last_used.pop(uid, None)

        if user_id not in _engines:
            _engines[user_id] = _new_engine()
        _engines_last_used[user_id] = now
        return _engines[user_id]


def clean(obj):
    """Recursively convert numpy/pandas scalar types to native Python and
    replace NaN/inf with None, so jsonify doesn't choke on either."""
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        val = float(obj)
        return None if (math.isnan(val) or math.isinf(val)) else val
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if obj is pd.NA or (isinstance(obj, float) and pd.isna(obj)):
        return None
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    return obj


def df_records(df):
    if df is None or len(df) == 0:
        return []
    return clean(df.replace({pd.NA: None}).to_dict("records"))


def _int_arg(name, default, lo, hi):
    """Parse an int query param, clamped to [lo, hi]. Falls back to
    default on anything non-numeric instead of raising."""
    raw = request.args.get(name, default)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def _require_loaded(engine):
    if not engine.loaded:
        return jsonify({"ok": False, "message": "No player data loaded yet. Upload a players.csv first."}), 400
    return None


# ----------------------------------------------------------------------
# Pages
# ----------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/season")
def season_page():
    return render_template("season.html")


# ----------------------------------------------------------------------
# Setup / league config
# ----------------------------------------------------------------------

@app.route("/api/upload", methods=["POST"])
def upload_players():
    engine = get_engine()
    file = request.files.get("file")
    if not file or file.filename == "":
        return jsonify({"ok": False, "message": "No file provided"}), 400
    if not file.filename.lower().endswith(".csv"):
        return jsonify({"ok": False, "message": "Please upload a .csv file"}), 400

    # Filename includes this visitor's session id so two people uploading
    # at once can't clobber each other's file on disk.
    save_path = os.path.join(UPLOAD_DIR, f"players_{session['user_id']}.csv")
    file.save(save_path)

    try:
        engine.load_players(save_path)
    except Exception as e:
        return jsonify({"ok": False, "message": f"Failed to parse CSV: {e}"}), 400

    return jsonify({"ok": True, "players_loaded": len(engine.players_df)})


@app.route("/api/ffc/sync", methods=["POST"])
def ffc_sync():
    """Pull the current player pool from Fantasy Football Calculator's ADP
    API for the given scoring format. Body: {"scoring":
    "standard"|"half_ppr"|"ppr", "teams": int}.

    There is deliberately no way to force a fresh fetch from this route -
    see ffc_source.get_players. FFC's data only updates once a day and
    they explicitly ask API users not to poll more often than that; this
    endpoint honors that regardless of what a request sends, request
    volume, or how many different people are using this site - the
    underlying cache is keyed by scoring/team-size only, not by session,
    so everyone hitting Sync draws from the same once-a-day fetch rather
    than each session triggering its own."""
    engine = get_engine()
    data = request.get_json(force=True) or {}
    scoring = data.get("scoring", "ppr")
    if scoring not in ffc_source.SCORING_SLUGS:
        return jsonify({
            "ok": False,
            "message": f"scoring must be one of {list(ffc_source.SCORING_SLUGS)}",
        }), 400
    try:
        teams = int(data.get("teams", engine.num_teams))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "teams must be a whole number"}), 400
    if not (2 <= teams <= 32):
        return jsonify({"ok": False, "message": "teams must be between 2 and 32"}), 400

    try:
        players, meta = ffc_source.get_players(scoring, teams=teams)
        engine.load_players_from_records(players, source_meta=meta)
    except requests.exceptions.RequestException:
        return jsonify({
            "ok": False,
            "message": "Couldn't reach Fantasy Football Calculator - check your internet connection and try again.",
        }), 502
    except Exception as e:
        return jsonify({"ok": False, "message": f"Sync failed: {e}"}), 400

    return jsonify({
        "ok": True,
        "players_loaded": len(engine.players_df),
        "meta": meta,
    })


@app.route("/api/ffc/movers")
def ffc_movers():
    """Players whose ADP has moved meaningfully since the oldest cached
    snapshot still on disk (up to a few days back - see
    ffc_source.RETENTION_DAYS). Returns an empty list, not an error, if
    there isn't at least two days of history yet."""
    engine = get_engine()
    scoring = request.args.get("scoring", "ppr")
    if scoring not in ffc_source.SCORING_SLUGS:
        return jsonify({
            "ok": False,
            "message": f"scoring must be one of {list(ffc_source.SCORING_SLUGS)}",
        }), 400
    teams = _int_arg("teams", engine.num_teams, 2, 32)
    min_delta = _int_arg("min_delta", 3, 1, 50)
    movers = ffc_source.get_adp_movers(scoring, teams=teams, min_delta=min_delta)
    return jsonify({"movers": movers})


@app.route("/api/settings", methods=["GET", "POST"])
def settings():
    engine = get_engine()
    if request.method == "POST":
        data = request.get_json(force=True) or {}

        def as_int_in_range(value, lo, hi, label):
            try:
                n = int(value)
            except (TypeError, ValueError):
                raise ValueError(f"{label} must be a whole number.")
            if not (lo <= n <= hi):
                raise ValueError(f"{label} must be between {lo} and {hi}.")
            return n

        try:
            structural_change = False
            if "num_teams" in data:
                num_teams = as_int_in_range(data["num_teams"], 2, 32, "Teams")
                if num_teams != engine.num_teams:
                    engine.num_teams = num_teams
                    structural_change = True
            if "rounds" in data:
                rounds = as_int_in_range(data["rounds"], 1, 40, "Rounds")
                if rounds != engine.rounds:
                    engine.rounds = rounds
                    structural_change = True

            # Changing your draft slot does NOT need to wipe the draft in
            # progress - only team-count/round changes do, since those change
            # the league's actual structure.
            if "my_team" in data:
                as_int_in_range(data["my_team"], 1, engine.num_teams, "My Draft Slot")
                engine.set_my_team(int(data["my_team"]))

            if "scoring" in data and data["scoring"]:
                engine.scoring = str(data["scoring"])[:100]

            if "starters" in data and isinstance(data["starters"], dict):
                engine.set_starters(data["starters"])

            if "aggressiveness" in data:
                engine.set_aggressiveness(data["aggressiveness"])
        except (TypeError, ValueError) as e:
            return jsonify({"ok": False, "message": str(e)}), 400

        if structural_change and engine.loaded:
            # Re-clamp my_team in case num_teams shrank below it.
            engine.set_my_team(engine.my_team)
            engine.league = engine._create_league()
            engine.draft_order = engine._snake_order()
            engine.reset_draft()

    return jsonify({
        "num_teams": engine.num_teams,
        "my_team": engine.my_team,
        "roster_size": engine.roster_size,
        "rounds": engine.rounds,
        "scoring": engine.scoring,
        "starters": engine.starters,
        "aggressiveness": engine.aggressiveness,
        "loaded": engine.loaded,
        "players_loaded": len(engine.players_df) if engine.loaded else 0,
        "source_meta": engine.source_meta,
    })


# ----------------------------------------------------------------------
# Draft state
# ----------------------------------------------------------------------

@app.route("/api/state")
def state():
    engine = get_engine()
    err = _require_loaded(engine)
    if err:
        return err
    return jsonify({
        "pick": engine.draft_state["pick"],
        "round": engine.draft_state["round"],
        "current_team": engine.get_current_team_number(),
        "my_team": engine.my_team,
        "on_the_clock": engine.get_current_team_number() == engine.my_team,
        "players_remaining": len(engine.get_available_players()),
        "history": engine.draft_state["history"][-15:],
        "draft_complete": engine.is_draft_complete(),
        "total_rounds": engine.rounds,
    })


@app.route("/api/roster")
def roster():
    engine = get_engine()
    err = _require_loaded(engine)
    if err:
        return err
    return jsonify({
        "roster": engine.get_team_roster(engine.my_team),
        "slots": engine.get_roster_slots(engine.my_team),
        "needs": engine.get_roster_needs(),
        "position_counts": engine.get_position_counts(),
    })


@app.route("/api/league")
def league():
    engine = get_engine()
    err = _require_loaded(engine)
    if err:
        return err
    teams = []
    for i, team in enumerate(engine.league, start=1):
        teams.append({
            "team": team["team"],
            "is_user": i == engine.my_team,
            "slots": engine.get_roster_slots(i),
        })
    return jsonify({"league": engine.league, "teams": teams})


@app.route("/api/board")
def board():
    engine = get_engine()
    err = _require_loaded(engine)
    if err:
        return err
    return jsonify(clean(engine.get_draft_board()))


@app.route("/api/available")
def available():
    engine = get_engine()
    err = _require_loaded(engine)
    if err:
        return err
    count = _int_arg("count", 25, 1, 500)
    df = engine.get_available_players().sort_values("rank").head(count)
    return jsonify({"players": df_records(df)})


@app.route("/api/reset", methods=["POST"])
def reset():
    engine = get_engine()
    err = _require_loaded(engine)
    if err:
        return err
    engine.reset_draft()
    return jsonify({"ok": True})


# ----------------------------------------------------------------------
# Draft actions
# ----------------------------------------------------------------------

@app.route("/api/pick", methods=["POST"])
def pick():
    engine = get_engine()
    err = _require_loaded(engine)
    if err:
        return err
    data = request.get_json(force=True) or {}
    player_name = data.get("player")
    if not player_name or not isinstance(player_name, str):
        return jsonify({"ok": False, "message": "player is required"}), 400
    result = engine.draftiq_pick(player_name)
    return jsonify(clean(result))


@app.route("/api/target", methods=["POST"])
def target():
    engine = get_engine()
    err = _require_loaded(engine)
    if err:
        return err
    data = request.get_json(force=True) or {}
    player_name = data.get("player")
    action = data.get("action", "add")  # "add" or "remove"
    if not player_name or not isinstance(player_name, str):
        return jsonify({"ok": False, "message": "player is required"}), 400
    if action == "remove":
        engine.remove_target_player(player_name)
    else:
        engine.add_target_player(player_name)
    return jsonify({
        "ok": True,
        "targets": sorted(engine.target_players),
        "avoids": sorted(engine.avoid_players),
    })


@app.route("/api/avoid", methods=["POST"])
def avoid():
    engine = get_engine()
    err = _require_loaded(engine)
    if err:
        return err
    data = request.get_json(force=True) or {}
    player_name = data.get("player")
    action = data.get("action", "add")  # "add" or "remove"
    if not player_name or not isinstance(player_name, str):
        return jsonify({"ok": False, "message": "player is required"}), 400
    if action == "remove":
        engine.remove_avoid_player(player_name)
    else:
        engine.add_avoid_player(player_name)
    return jsonify({
        "ok": True,
        "targets": sorted(engine.target_players),
        "avoids": sorted(engine.avoid_players),
    })


@app.route("/api/flags")
def flags():
    """Current target/avoid lists, for the UI to mark player rows on load
    (e.g. after a page refresh) without re-deriving them from anywhere
    else."""
    engine = get_engine()
    err = _require_loaded(engine)
    if err:
        return err
    return jsonify({
        "targets": sorted(engine.target_players),
        "avoids": sorted(engine.avoid_players),
    })


@app.route("/api/simulate_to_me", methods=["POST"])
def simulate_to_me():
    engine = get_engine()
    err = _require_loaded(engine)
    if err:
        return err
    picks = engine.simulate_opponents_until_user()
    return jsonify({"ok": True, "picks": picks, "state": {
        "pick": engine.draft_state["pick"],
        "round": engine.draft_state["round"],
        "current_team": engine.get_current_team_number(),
    }})


@app.route("/api/start_draft", methods=["POST"])
def start_draft():
    """Resets the draft, then simulates opponent picks up to the user's
    first turn (a no-op if the user's slot picks first)."""
    engine = get_engine()
    err = _require_loaded(engine)
    if err:
        return err
    engine.reset_draft()
    picks = engine.simulate_opponents_until_user()
    return jsonify({"ok": True, "picks": picks, "state": {
        "pick": engine.draft_state["pick"],
        "round": engine.draft_state["round"],
        "current_team": engine.get_current_team_number(),
    }})


@app.route("/api/draft_any", methods=["POST"])
def draft_any():
    """Assign a pick to whichever team is currently on the clock (for
    manually running a live draft where opponents pick for themselves)."""
    engine = get_engine()
    err = _require_loaded(engine)
    if err:
        return err
    data = request.get_json(force=True) or {}
    player_name = data.get("player")
    if not player_name or not isinstance(player_name, str):
        return jsonify({"ok": False, "message": "player is required"}), 400
    result = engine.draft_player(player_name)
    return jsonify(clean(result))


@app.route("/api/undo", methods=["POST"])
def undo():
    engine = get_engine()
    err = _require_loaded(engine)
    if err:
        return err
    result = engine.undo_last_pick()
    return jsonify(clean(result))


# ----------------------------------------------------------------------
# Intelligence endpoints
# ----------------------------------------------------------------------

@app.route("/api/recommendations")
def recommendations():
    engine = get_engine()
    err = _require_loaded(engine)
    if err:
        return err
    count = _int_arg("count", 10, 1, 100)
    df = engine.get_draft_recommendations(count)
    return jsonify({"recommendations": df_records(df)})


@app.route("/api/decision")
def decision():
    engine = get_engine()
    err = _require_loaded(engine)
    if err:
        return err
    count = _int_arg("count", 10, 1, 100)
    df = engine.draftiq_decision_engine(count)
    return jsonify({"decisions": df_records(df)})


@app.route("/api/on_clock")
def on_clock():
    engine = get_engine()
    err = _require_loaded(engine)
    if err:
        return err
    result = engine.draftiq_on_clock_decision()
    return jsonify(clean(result))


@app.route("/api/alerts")
def alerts():
    engine = get_engine()
    err = _require_loaded(engine)
    if err:
        return err
    return jsonify({
        "draft_alerts": engine.get_draft_alerts(),
        "position_runs": engine.get_position_run_alerts(),
        "target_alerts": engine.get_target_alerts(),
    })


@app.route("/api/compare", methods=["POST"])
def compare():
    engine = get_engine()
    err = _require_loaded(engine)
    if err:
        return err
    data = request.get_json(force=True) or {}
    names = data.get("players", [])
    if not isinstance(names, list):
        return jsonify({"ok": False, "message": "players must be a list of names"}), 400
    df = engine.compare_players(names)
    return jsonify({"comparison": df_records(df)})


@app.route("/api/opportunity_cost", methods=["POST"])
def opportunity_cost():
    engine = get_engine()
    err = _require_loaded(engine)
    if err:
        return err
    data = request.get_json(force=True) or {}
    names = data.get("players", [])
    if not isinstance(names, list):
        return jsonify({"ok": False, "message": "players must be a list of names"}), 400
    df = engine.opportunity_cost(names)
    return jsonify({"opportunity_cost": df_records(df)})


@app.route("/api/strategy_report", methods=["POST"])
def strategy_report():
    engine = get_engine()
    err = _require_loaded(engine)
    if err:
        return err
    data = request.get_json(force=True) or {}
    names = data.get("players")
    if names is not None and not isinstance(names, list):
        return jsonify({"ok": False, "message": "players must be a list of names"}), 400
    result = engine.draft_strategy_report(names)
    return jsonify(clean(result))


# ----------------------------------------------------------------------
# Season management: start/sit, waivers, trades
# ----------------------------------------------------------------------
# The league itself (team rosters, num_teams, starters) is shared across
# everyone using this deployment - a real league is one shared reality,
# not something each visitor should re-enter. Only "which team is mine"
# is per-visitor, stored in the session alongside the draft user_id.

def _my_season_team():
    return session.get("season_my_team", 1)


@app.route("/api/season/league", methods=["GET", "POST"])
def season_league():
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        try:
            num_teams = int(data["num_teams"]) if "num_teams" in data else None
            if num_teams is not None and not (2 <= num_teams <= 32):
                return jsonify({"ok": False, "message": "num_teams must be between 2 and 32"}), 400
        except (TypeError, ValueError):
            return jsonify({"ok": False, "message": "num_teams must be a whole number"}), 400
        starters = data.get("starters")
        if starters is not None and not isinstance(starters, dict):
            return jsonify({"ok": False, "message": "starters must be an object"}), 400
        season.update_league_settings(num_teams=num_teams, starters=starters)

    league = season.load_league()
    league["my_team"] = _my_season_team()
    return jsonify(league)


@app.route("/api/season/my_team", methods=["POST"])
def season_my_team():
    data = request.get_json(force=True) or {}
    league = season.load_league()
    try:
        team_number = int(data.get("team_number"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "team_number must be a whole number"}), 400
    if str(team_number) not in league["teams"]:
        return jsonify({"ok": False, "message": f"Team {team_number} doesn't exist in this league"}), 400
    session["season_my_team"] = team_number
    return jsonify({"ok": True, "my_team": team_number})


@app.route("/api/season/team/<int:team_number>", methods=["POST"])
def season_update_team(team_number):
    data = request.get_json(force=True) or {}
    name = data.get("name")
    roster = data.get("roster")
    if roster is not None and not isinstance(roster, list):
        return jsonify({"ok": False, "message": "roster must be a list of player names"}), 400
    try:
        team = season.update_team_roster(team_number, name=name, roster=roster)
    except ValueError as e:
        return jsonify({"ok": False, "message": str(e)}), 400
    return jsonify({"ok": True, "team": team})


@app.route("/api/season/projections/upload", methods=["POST"])
def season_upload_projections():
    file = request.files.get("file")
    if not file or file.filename == "":
        return jsonify({"ok": False, "message": "No file provided"}), 400
    if not file.filename.lower().endswith(".csv"):
        return jsonify({"ok": False, "message": "Please upload a .csv file"}), 400

    week_key = request.form.get("week", "season")
    if week_key != "season":
        try:
            week_key = int(week_key)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "message": "week must be a whole number or 'season'"}), 400

    try:
        df = pd.read_csv(file)
        records = season.normalize_projection_records(df)
    except Exception as e:
        return jsonify({"ok": False, "message": f"Failed to parse CSV: {e}"}), 400

    season.save_projections_for_week(week_key, records)
    return jsonify({"ok": True, "week": week_key, "players_loaded": len(records)})


@app.route("/api/season/start_sit")
def season_start_sit():
    week_key = request.args.get("week", "season")
    if week_key != "season":
        week_key = _int_arg("week", 1, 1, 30)
    try:
        result = season.get_start_sit(_my_season_team(), week_key)
    except ValueError as e:
        return jsonify({"ok": False, "message": str(e)}), 400
    return jsonify(result)


@app.route("/api/season/waivers")
def season_waivers():
    week_key = request.args.get("week", "season")
    if week_key != "season":
        week_key = _int_arg("week", 1, 1, 30)
    min_improvement = _int_arg("min_improvement", 3, 0, 50)
    try:
        result = season.get_waiver_recommendations(_my_season_team(), week_key, min_improvement=min_improvement)
    except ValueError as e:
        return jsonify({"ok": False, "message": str(e)}), 400
    return jsonify(result)


@app.route("/api/season/trades")
def season_trades():
    week_key = request.args.get("week", "season")
    if week_key != "season":
        week_key = _int_arg("week", 1, 1, 30)
    try:
        result = season.get_trade_suggestions(_my_season_team(), week_key)
    except ValueError as e:
        return jsonify({"ok": False, "message": str(e)}), 400
    return jsonify(result)


# ----------------------------------------------------------------------
# Error handlers - always return JSON, never an HTML stack-trace page.
# With debug off (see bottom of file), an uncaught exception in a route
# would otherwise show a bare "Internal Server Error" page; the frontend
# expects JSON from every /api/ call, so give it that consistently.
# ----------------------------------------------------------------------

@app.errorhandler(400)
def _handle_400(e):
    return jsonify({"ok": False, "message": "Bad request."}), 400


@app.errorhandler(404)
def _handle_404(e):
    return jsonify({"ok": False, "message": "Not found."}), 404


@app.errorhandler(413)
def _handle_413(e):
    return jsonify({"ok": False, "message": "File too large (8MB max)."}), 413


@app.errorhandler(500)
def _handle_500(e):
    return jsonify({"ok": False, "message": "Something went wrong on the server."}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    try:
        # waitress is a lightweight, cross-platform production WSGI server -
        # a personal/distributed build shouldn't run on Flask's own dev
        # server, which prints its own warning about exactly that.
        from waitress import serve
        print(f"DraftIQ is running - open http://127.0.0.1:{port} in your browser.")
        print("Press Ctrl+C to stop.")
        serve(app, host="0.0.0.0", port=port)
    except ImportError:
        # Fallback if waitress isn't installed. debug is always off here:
        # Flask's debug mode ships an interactive in-browser debugger that
        # can execute arbitrary code on whatever machine is running it -
        # never appropriate for anything you hand to someone else.
        print("(waitress not installed - falling back to Flask's dev server. "
              "Run `pip install waitress` for a sturdier local server.)")
        app.run(host="0.0.0.0", port=port, debug=False)
