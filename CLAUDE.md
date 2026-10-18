# CLAUDE.md

Django app where AI agents playtest competitive Magic: The Gathering decks (Standard,
Pioneer, Modern, Legacy, Vintage, Pauper, Duel Commander; Commander decks import but
two-seat play only) under real rules: Forge, behind the
[`mtg_forge_engine`](https://github.com/gab-25/mtg_forge_engine) bridge, plays every
game, and Jev plays every seat, picking among its options (users pick decks, not
agents). Start
from `README.md` (features, rules, configuration); details live in `docs/`.

## Commands

```bash
uv sync                                    # install deps (dev group included)
uv run pytest                              # full suite, hermetic, ~seconds
FORGE_URL=http://localhost:8080 uv run pytest -m forge  # opt-in: against a real bridge
uv run python manage.py migrate            # apply pending migrations
uv run python manage.py runserver          # local dev server on :8000, auto-reload
uv run python manage.py makemigrations     # after model/choices changes
uv run python manage.py load_oracle_cards  # fill the card cache from Scryfall bulk data
uv run python manage.py sync_metagame      # import the top decks of each format (network)
uv run python manage.py export_match <id> --format md  # a match and its decision traces, for review
uv run python manage.py export_playtest <id> --output DIR  # every match of a playtest, one file each
uv run python manage.py benchmark <match id> --games 20 --seed N  # Jev vs Forge AI with a match's decks
uv run python manage.py benchmark_report <playtest id> ...  # Jev's numbers over benchmark sessions
docker compose up --build                  # Postgres + Forge + Redis + gunicorn on :8000 + the worker
docker compose up -d db                    # just Postgres, for local runs
docker compose up -d forge                 # just the Forge bridge, on localhost:8080
docker compose up -d redis                 # just Redis, the Celery broker, on localhost:6379
uv run celery -A mtg_gauntlet worker --pool threads -l info  # the worker that plays matches
```

The default `DATABASE_URL` is the Compose Postgres on localhost; set
`DATABASE_URL=sqlite:///./mtg.db` to run without it. `FORGE_URL` defaults to
`http://localhost:8080`. Tests always use in-memory SQLite
(`mtg_gauntlet/settings_test.py`).

## Layout

- `mtg_gauntlet/` — settings, URLs, layout templates, the Celery app, deck-import
  threads, ownership.
- `decks/` — decklist parsing, Scryfall fetch + cache, legality, format registry →
  `docs/deck-import.md`.
- `playtest/forge.py` — the HTTP client of the Forge bridge → `docs/forge.md`.
- `playtest/views.py`, `playtest/forms.py` — a playtest session: the deck to test
  plays `games_per_opponent` matches against each chosen opponent.
- `playtest/runner.py` — starts a match's game on the bridge, copies its events into
  `MatchEvent` rows, stores the result and the summary (`playtest/summary.py`).
- `playtest/tasks.py` — the Celery task following a match, and the worker's hooks
  (pool start, recovery) → `docs/forge.md`.
- `playtest/workers.py` — the worker pool answering the bridge's decisions with each
  seat's agent, storing a `DecisionTrace` per decision Jev is asked → `docs/forge.md`.
- `playtest/checks.py` — deterministic checks that flag suspicious decisions;
  `playtest/export.py` — a match as JSON lines or Markdown; `playtest/benchmark.py` —
  Jev against Forge's AI on fixed seeds, and its report → `docs/agents.md`.
- `playtest/agents/` — the Jev agent and its client →
  `docs/agents.md`.
- `theme/static/` — the hand-written stylesheet, vendored fonts and icons.
- Overall flow (request → Celery task (matches) or thread (imports) → stored result,
  HTMX polling) → `docs/architecture.md`; tests, Docker, migrations →
  `docs/development.md`.

## Conventions

- Code, comments, docstrings, commit messages, README, `docs/` and issues are in
  English.
- Forge is the rules engine: never re-implement rules or card effects here. An agent
  only picks one of the bridge's options (by index), or rates the positions Forge
  simulated for them, or delegates to Forge's AI; no model is ever the rules engine.
- `decks/formats.py` is the single source for format parameters (its `forge_format`
  is the bridge's name). Adding a format, a `Seat.Agent` or any status/choice value
  needs a migration.
- Templates may only use CSS classes defined in the hand-written stylesheets in
  `theme/static/css/` — `styles.css` (tokens in `:root`, reset, base), `layout.css`
  (app shell, page furniture), `components.css`, linked in that order by
  `partials/stylesheets.html`; no Tailwind, no build — and icon names the vendored Material Symbols subset can render
  (no `j`/`q`), each icon with `translate="no"`. `tests/test_assets.py` enforces both;
  keep `KNOWN_UNCOMPILED` empty. Every page extends `base.html` (the sidebar); its
  section comes from the route name (`nav_section`).
- Tests must never call Scryfall, OpenRouter or the Forge bridge (`tests/conftest.py`
  fails any that does): use the `fake_forge` fixture (`tests/fakeforge.py`),
  monkeypatch `fetch_card_data` / `fetch_card_raw`, `requests` or
  `typesafe.system_one`, or pass a fake `ask`. Only tests marked `forge`
  reach a real bridge. Celery tasks run eagerly in tests and the broker is refused
  (`tests/conftest.py`). Shared builders are in `tests/factories.py`.
- Every deck/match view loads rows through `mtg_gauntlet.access.get_owned_or_404`.

## Workflow

- One branch per issue (`feature/<name>`, `bugfix/<name>`), PR to `main`.
- CI runs only on pushes to `main`: run `uv run pytest` before opening a PR.
- `docs/superpowers/` (working specs and plans) is gitignored: never commit it.
