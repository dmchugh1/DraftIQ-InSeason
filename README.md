# DraftIQ

A personal fantasy football draft assistant: live recommendations, tiers,
scarcity/urgency alerts, targets/avoids, and draft-board tracking, run
entirely on your own machine — plus a separate in-season page for
start/sit, waiver, and trade recommendations once the draft is done.

## File layout

```
app.py
engine.py
ffc_source.py
season.py
requirements.txt
templates/
  index.html      <- the Draft Room page
  season.html     <- the Season page
uploads/           <- created automatically (uploaded CSVs)
season_data/       <- created automatically (saved league/projections)
```

`index.html` and `season.html` must be inside a `templates/` folder next
to `app.py` — that's a Flask requirement (`render_template` only looks
there), not optional structure.

## Setup

1. Install Python 3.10+.
2. From this folder, install dependencies:
   ```
   pip install -r requirements.txt
   ```

## Running it

```
python app.py
```

Then open **http://127.0.0.1:5000** for the Draft Room, or
**http://127.0.0.1:5000/season** for in-season tools. Stop the server
with `Ctrl+C`.

By default it serves on port 5000. To use a different port:

```
PORT=8000 python app.py
```

## Loading your rankings

`uploads/sample_players.csv` is a **format template only** — placeholder
names ("QB Example 1", etc.), not real players or real rankings. It exists
so the app has something to show you immediately and so you can see the
exact column layout expected.

Before you draft, replace it with a real rankings export (from
FantasyPros, ESPN, your own spreadsheet, etc.) using the **Setup** tab's
upload box. Required columns:

| column | notes |
|---|---|
| `name` (or `player`/`player_name`) | player's name |
| `position` (or `pos`) | QB/RB/WR/TE/DEF/K, etc. |
| `rank` | overall rank — required, rows without one are dropped |
| `adp` | average draft position — optional but recommended |
| `team` | optional |

### Or sync from Fantasy Football Calculator instead

The **Setup** tab also has a "Sync from Fantasy Football Calculator"
option that pulls live ADP rankings directly — no CSV needed. Pick a
scoring format (Standard / Half PPR / Full PPR) and your team count,
then hit **Sync Rankings**.

A player's **rank** is always their position in that sorted list (1st,
2nd, 3rd...) — never the raw ADP number, which is a fractional average
(e.g. 24.4) and isn't the same thing.

A few things worth knowing:

- **Cached once a day.** Fantasy Football Calculator's own ADP data only
  updates once a day, and they ask API users not to poll more often than
  that. DraftIQ respects this automatically — a sync within 24 hours of
  the last one for that scoring format/team-size just reuses what's
  already cached, unless you check **Force Refresh**.
- **A few days of history are kept** (per scoring format + team size)
  specifically so ADP movement can be tracked over time, not just the
  latest snapshot.
- **ADP Movers**, shown on the on-clock (Draft Room) screen, flags
  players whose ADP has shifted meaningfully since the oldest snapshot
  still in that history — rising (being drafted earlier than before) or
  falling (being drafted later). This needs at least two days of synced
  data to show anything; right after your first sync it'll be empty,
  which is expected.
- This integration was written against Fantasy Football Calculator's
  published API docs and sample data, but hasn't been exercised against
  a live call in the environment it was built in (no outbound network
  access there). If the sync ever fails outright rather than just
  returning stale-looking data, that's the first thing to check —
  compare the actual JSON shape at
  `https://fantasyfootballcalculator.com/api/v1/adp/ppr?teams=12`
  against what `ffc_source.py`'s `_normalize()` function expects.

## Season page

At `/season` — a separate page for once the draft is over, so you're
not stuck flipping back to the draft screen all year. Three tools:

- **Start/Sit** — your optimal lineup for a given week, computed fresh
  from your saved roster and uploaded projections (not a saved "who's
  starting" state — it always recalculates the best lineup, including
  filling FLEX with whichever eligible player projects highest, not
  locked to one position).
- **Waiver Wire** — bench players with a meaningfully higher-projected
  free agent (unrostered by anyone in the league) available at the same
  position.
- **Trades** — positions where another saved team's surplus lines up
  with your need, and vice versa. This is a heuristic starting point
  for a conversation, not a value-balanced trade calculator.

**League rosters are shared**, the same way the FFC ADP cache is shared
— a real league is one shared reality, not something each visitor
should re-enter separately. Save each team's roster once (League &
Rosters, on the Season page) and everyone using this deployment for the
same league sees the same data. Each visitor just picks which team
number is theirs — that part is personal and remembered per-session,
the same way the draft tool remembers your draft slot.

**Projections are CSV uploads** (Season page → Upload Projections),
matching the same loose column-matching as the draft CSV importer:
`name`/`player`, `position`/`pos`, `team`, and a projection column
(`proj_points`, `points`, `proj`, or `fpts`). Upload per-week for
start/sit and waivers, or as "Season-long" for rest-of-season trade
value. This is designed to be easy to feed from a separate projections
pipeline (e.g. nflverse-based) once you have one - just export to that
column shape.

Data lives in `season_data/` on disk (no database), the same
in-memory/single-process caveats as everything else in this app apply.

## Multi-user support

Each visitor gets their own private draft, isolated by a signed browser
cookie (no login required) — one deployment can be shared with your
whole league, and everyone's settings, roster, and draft progress stay
separate.

What this means in practice:

- **No accounts, in-memory only.** There's no database — each session's
  draft state lives in server memory for as long as the process runs.
  A server restart clears everyone's progress, same as it always did
  for a single user.
- **Idle sessions get cleaned up automatically**, 6 hours after the last
  request, so memory doesn't grow unbounded over a long-running
  deployment. No action needed on your end.
- **Single process only.** This works correctly as long as the app runs
  as one process (Render's free/starter tier, or `python app.py`
  locally). If you ever move to a host that runs multiple instances or
  auto-scales, this in-memory approach breaks — each instance would
  have its own separate session store, splitting one visitor's requests
  unpredictably. That would need a real shared store (Redis, a
  database) instead — ask if you get there.
- **The Fantasy Football Calculator sync stays shared, not per-user** —
  see the sync section above. Only draft state is isolated per visitor.

## Data

Nothing here leaves your machine or your chosen host — no accounts, no
analytics, no third-party calls beyond the optional Fantasy Football
Calculator sync you trigger yourself. Uploaded CSVs and draft progress
live only in server memory and this folder.

