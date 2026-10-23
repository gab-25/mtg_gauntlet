# Architecture

MTG Gauntlet is one Django project (`mtg_gauntlet`) with three apps:

| App | What it owns |
|---|---|
| `mtg_gauntlet/` | Settings, root URLs, the layout templates and the home page, plus the plumbing every app shares: the Celery app (`celery.py`), deck-import threads (`jobs.py`), the ownership check (`access.py`), per-job log stamping (`logging_context.py`). |
| `decks/` | Decks and the Scryfall card cache: decklist parsing, card fetching, legality, the format registry. See [Deck import](deck-import.md). |
| `playtest/` | Matches: the client of the Forge bridge (the rules engine), the runner, the Celery task (`tasks.py`) and the worker pool that connect it to the database, the agents that play, and the match summary. See [Forge](forge.md) and [Agents](agents.md). |
| `theme/` | Static assets only: the precompiled stylesheet, vendored fonts, favicons. No Python beyond its `AppConfig`. |

## Request → background job → stored result

Both long-running operations — importing a deck and playing a match — follow the
same shape:

```
POST form ──► cheap validation ──► row created (status = pending) ──► start_job / .delay()
   │                                                                     │
   └─► redirect to a page that polls (HTMX) ◄── row updated as it runs ◄─┘
```

1. **The view validates what it can synchronously** and re-renders the form with
   status 422 on errors (`decks.importer.decklist_errors`,
   `playtest.forms.parse_playtest_form`).
2. **It creates the rows** (`Deck` with `status=pending`; for a playtest, the
   `Playtest` session and `games_per_opponent` `Match`es per opponent, each with its
   two `Seat`s, in one transaction), then starts the import with
   `start_job(run_import, id)`, or queues each match with
   `playtest.tasks.run_match_task.delay(id)`, game by game across the opponents (every
   opponent's first game, then every second game…) so each matchup gets going early.
   A playtest first starts the first game of each opponent on the Forge bridge
   (`playtest.runner.start_game`), which checks the decks once per opponent: when Forge
   does not know a card (or the bridge is unreachable) the games already started are
   aborted, the session is deleted and the form is re-rendered naming the cards, so a
   refused deck never becomes a playtest. The other games start on the bridge when
   the worker picks them up.
   If the broker refuses a match, the same happens as for a bridge error (503).
3. **Where the job runs.** A deck import runs in a daemon thread of the web process
   (`RUN_JOBS_IN_BACKGROUND=1`, the default) or inline (tests); moving it to the
   worker is a follow-up. A match runs on the **Celery worker**: Redis carries the
   task, a single worker process (`--pool threads`, `CELERY_CONCURRENCY` threads,
   default 16) follows up to that many matches at once and the rest wait in the
   queue, first in first out: a session's games queue behind the sessions started
   before it, which is why a session plays at most `playtest.forms.MAX_GAMES` (50). Restarting the web process does not touch them. Tests run tasks eagerly
   (`CELERY_TASK_ALWAYS_EAGER`), with no broker. The database, not the queue, is the
   source of truth: when the worker starts it re-queues every `pending` or `running`
   match, and a task whose match ended, was stopped or deleted, or is already
   followed, does nothing.
4. **The job writes its progress and outcome back to the row**:
   - `decks.importer.run_import` moves the deck `pending → processing → ready | failed`
     and stores `cards`, `commander`, `color_identity` (or `error`).
   - `playtest.runner.run_match` moves the match `pending → running → finished |
     stopped | failed`, copies one `MatchEvent` per bridge event as it arrives, keeps
     `Match.turn` current, and at the end stores the winner, Forge's end reason, each
     seat's final life and the deterministic `Match.summary`.
   - Meanwhile the **worker pool** (`playtest/workers.py`, `FORGE_WORKERS` threads
     started once in the Celery worker, when it is ready) answers the bridge's decisions
     for every running match, storing a `DecisionTrace` for every decision Jev is asked
     (the runner links each to its event and settles its outcome when the match ends).
     Without a pool the runner answers its own.
5. **The page polls**. The deck list and playtest list re-fetch their grid partial while
   anything is busy (`has_busy` in the context; the view returns the partial when
   `request.htmx` is set). A playtest page polls `playtests/<id>/live` (each
   opponent's record when it plays several games, and one row per game); a match page polls `matches/<id>/live`, which renders
   the scoreboard, the log and the decision inspector (the **Decisions** tab: every
   decision trace, filtered by seat, kind, fallback, flags or outcome; each opens
   `matches/<id>/decisions/<trace id>`, the decision as Jev saw it).

Every log line emitted inside a job is prefixed with `[deck <id>]` or
`[match <id>]` (`job_log_context` + the `JobFilter` logging filter), so concurrent
jobs can be told apart on the console.

## Boundaries

- **Forge is the rules engine.** Rules and card effects live in Forge, behind the
  `mtg_forge_engine` bridge; this app never re-implements them.
  `playtest/forge.py` is the only module that speaks HTTP to the bridge.
- **No model is the rules engine.** Forge lists the legal options of each
  decision; an agent only picks one of them by index, or delegates to Forge's AI.
- **Agents only see what the bridge hands them**: the seat's filtered view, the
  decision kind, Forge's prompt and the numbered options with their facts.
- **`decks/rules/` is pure too**: parsing and legality are functions over plain
  dicts, no I/O. The importer is the one place that combines them with Scryfall and
  the cache.
- **Formats are data.** `decks/formats.py` holds the registry both apps read: deck
  legality uses `legality_key` and the family's construction rules, the match form
  uses the seat counts and `forge_format` (the format's name on the bridge, `None`
  for one it cannot seat), the summary reads `starting_life`.

## Data model

| Table | Model | Notes |
|---|---|---|
| `decks` | `decks.Deck` | UUID primary key. `cards` is a JSON list of `{"quantity", "is_commander", "board"?, "data"}` where `data` is the processed Scryfall card (see [Deck import](deck-import.md#stored-card-shape)) and `board` is `"side"` for a constructed sideboard card. |
| `scryfall_cards` | `decks.ScryfallCard` | Raw Scryfall JSON keyed by `card_en_<slug>`, shared by every user. `{"error": "not_found"}` caches a miss. |
| `scryfall_bulk_loads` | `decks.BulkDataLoad` | One row per `load_oracle_cards` run (for `--if-newer`). |
| `metagame_decks` | `decks.MetagameDeck` | The top decks of each format, replaced by every `sync_metagame`; shared and read-only, with source, archetype, meta share, rank, cards and `coverage` — Forge's check (`{"unresolved", "unsupported_cards"}`), empty until a playtest first starts against it: the sync does not ask Forge. |
| `playtests` | `playtest.Playtest` | A test session: UUID primary key, owner, format, the deck under test (`deck`, `SET_NULL`, with `deck_name`), `seed`, `games_per_opponent` (default 1), `agent` (who plays every seat: `jev`, the default, or `forge_ai`). Its matches (`games_per_opponent` per opponent) are deleted with it. |
| `matches` | `playtest.Match` | UUID primary key, `playtest` (empty for matches from before sessions; seat 0 is the deck under test, seat 1 the opponent), `seed` (`session seed + opponent index × games_per_opponent + game index`: unique in the session, and it locates the match's opponent and game), `forge_game_id`, `turn`, status (`pending`/`running`/`finished`/`failed`/`stopped`), winner (a `Seat`), `end_reason` (Forge's, verbatim), `started_at`, `summary` (JSON). |
| `seats` | `playtest.Seat` | Position, agent (the playtest's: `jev` or `forge_ai`), a model id (older seats only; new seats use `~typesafe/jev-latest`), the deck it plays — a user `deck` or a `metagame_deck` — `coverage` (`{"unsupported_cards": [...]}` from the bridge) and final `life`. `deck` is `SET_NULL` on delete; `deck_name` keeps the name. |
| `match_events` | `playtest.MatchEvent` | The bridge's log: `seq`, `turn`, `step` (Forge's phase), `seat_position`, `kind` (`log`/`decision`/`fallback`/`forge_ai`/`game_over`), `text`, `payload` (Forge's log category, Jev confidence and probabilities, fallback reasons), `reasoning`. |

**Ownership.** `Deck` and `Match` have an `owner`. Every view loads them through
`mtg_gauntlet.access.get_owned_or_404`, which answers 404 (not 403) for someone
else's row. Login is required everywhere except the login page.

## Web layer

- Views are plain function views with `@login_required` and
  `@require_http_methods`.
- Templates: project-wide ones (layout, login, shared partials) live in
  `mtg_gauntlet/templates/`; each app keeps its own in
  `<app>/templates/<app>/`, with HTMX fragments under `partials/`.
- HTMX comes from `django-htmx` (`{% htmx_script %}` in `base.html`; the middleware
  sets `request.htmx`).
- Layout: every page extends `base.html`, a fixed sidebar (New playtest, Home ·
  Decks · Playtests · Metagame, the engine box, the account) beside the page. The
  active section comes from the route name through the `nav_section` filter
  (`mtg_gauntlet/templatetags/`, loaded as a builtin), the counts and engine state
  from the `sidebar` context processor (local data only: no call to the bridge). The
  Forge bridge row loads apart (`/engine/forge`, HTMX on load, then every 30s): it
  asks the bridge's `/health` with a 2s timeout and caches the answer for 10s. The
  Jev row does the same (`/engine/jev`, every 5 minutes, cached 60s) with
  OpenRouter's `/api/v1/key`, which spends no credit: it tells whether the key works
  and has credit left, not whether Jev itself answers. Without a key it asks nothing.
  The Workers row (the user's running matches over `FORGE_WORKERS`) reloads from
  `/engine/workers` every 10s, as a session's matches start and end.
  Pages put their breadcrumb in `{% block breadcrumb %}`; tabs are links with
  `?tab=` (deck: results/cards/list, match: summary/log).
- Routes: `/decks/<id>`, `/playtests`, `/playtests/new?deck=&opponents=&format=`,
  `/playtests/<id>` and its matches at `/playtests/<id>/matches/<id>` (a match is
  found only under its own playtest), `/metagame?format=`.
- Numbers shown per deck and per opponent come from `playtest/stats.py`: pure
  functions over matches already loaded, seen from seat 0, finished matches only.
- Styling is **hand-written** CSS in `theme/static/css/`: `styles.css` (design tokens
  in `:root`, reset, base), `layout.css` (app shell, page furniture, login) and
  `components.css`, linked in that order by `partials/stylesheets.html`. No Tailwind
  and no build step.
  Icons are Material Symbols ligatures from a vendored font subset. See
  [Development](development.md#templates-styles-and-icons) for the constraints this
  puts on templates.
- Static files are served by WhiteNoise (hashed and compressed when `DEBUG` is off).
- Card images are never stored: `image_urls` point at `cards.scryfall.io`, and the
  zoom modal endpoint (`card-image`) only accepts URLs under that prefix.
