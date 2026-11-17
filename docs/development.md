# Development

The project runs on Python 3.14 and uses [`uv`](https://docs.astral.sh/uv/) for
dependencies (`pyproject.toml`, `uv.lock`). It is an application, not a package
(`[tool.uv] package = false`): uv installs the dependencies and the code runs from
the working directory.

## Running locally

```bash
uv sync                                   # dependencies, dev group included
cp .env.example .env                      # then edit it
docker compose up -d db                   # optional: just Postgres, on localhost:5432
docker compose up -d forge                # the Forge bridge, on localhost:8080
docker compose up -d redis                # the Celery broker, on localhost:6379
uv run python manage.py migrate
uv run python manage.py createsuperuser
uv run python manage.py runserver         # http://localhost:8000, auto-reload
uv run celery -A mtg_gauntlet worker --pool threads -l info   # plays the matches (second terminal)
```

Matches need the worker: `runserver` alone queues them and they stay *Pending*. The
worker does not auto-reload: restart it after changing `playtest/`.

The default `DATABASE_URL` points at the Compose Postgres
(`postgresql://mtg:mtg@localhost:5432/mtg_tester`). For a database-free setup use
SQLite instead, e.g. `DATABASE_URL=sqlite:///./mtg.db` (`*.db` is gitignored).

Matches need the Forge bridge. `FORGE_URL` defaults to `http://localhost:8080`, where
Compose publishes it; its first build compiles Forge (several minutes) and its card
database loads in about a minute before `/health` answers. Without it decks import
and pages work, but a new match is refused with "Forge bridge unreachable".

Everything is configured through environment variables; a `.env` file is loaded by
`mtg_gauntlet/__init__.py` (real environment variables win). The full table is in
the README's *Configuration* section.

## Running with Docker

```bash
docker compose up --build
docker compose exec web python manage.py createsuperuser
```

- `db`: `postgres:16-alpine`, data in the `pgdata` volume, published on 5432.
- `engine`: the bridge, the `ghcr.io/gab-25/mtg_forge_engine:latest` image
  (`docker compose pull forge` to update it), published on
  `127.0.0.1:8080` only (it has no authentication). `MAX_TURNS` and
  `MAX_CONCURRENT_GAMES` pass through from your shell.
- `web`: the `Dockerfile` image — `uv sync --frozen --no-dev`, `collectstatic` at
  build time, `DEBUG=0`, gunicorn on port 8000 as an unprivileged user.
  `docker-entrypoint.sh` runs `migrate` before starting the server. It waits for
  the bridge's health check and reaches it at `FORGE_URL=http://forge:8080`.
- `redis`: `redis:7-alpine`, the Celery broker, published on `127.0.0.1:6379`, no
  volume (the worker rebuilds the queue from the database when it starts).
- `worker`: the same image running `celery -A mtg_gauntlet worker --pool threads`;
  it follows the matches. Keep one replica (it drops a duplicate delivery of a match
  it already follows). It starts once `web` answers its health check, so migrations
  are applied by then.
- `CELERY_CONCURRENCY` is passed through from your shell.

CI (`.github/workflows/ci.yml`) runs on pushes to `main` only: `uv sync --frozen`,
`uv run pytest`, then builds and pushes a multi-arch image to
`ghcr.io/<owner>/<repo>` (`latest` and `sha-…` tags). Pull requests are not tested
by CI — run the suite locally before opening one.

## Tests

```bash
uv run pytest                     # whole suite, a few seconds
uv run pytest tests/test_runner.py -k seed
FORGE_URL=http://localhost:8080 uv run pytest -m forge   # opt-in: against a real bridge
```

The suite is **hermetic**:

- `pytest.ini_options` selects `mtg_gauntlet.settings_test`: in-memory SQLite,
  independent of `.env` and `DATABASE_URL`.
- `RUN_JOBS_IN_BACKGROUND = False`: imports and matches run inline, so a test can
  POST a form and assert on the finished row.
- **No network.** Tests never reach Scryfall or the Forge bridge. They monkeypatch
  `decks.importer.fetch_card_data` / `decks.scryfall.fetch_card_raw`, or
  `requests.get` / `requests.post` on the module under test. `tests/conftest.py` makes any
  request that reaches the transport fail the test; only tests marked `forge` are
  let through, and they skip without `FORGE_URL`.
- **The fake bridge.** The `fake_forge` fixture routes every `playtest.forge.client()`
  call to one `tests/fakeforge.py::FakeForge`: scripted two-seat games (mulligans,
  lands, Shocks, an attacker and a blocker a turn) played by "Forge's AI" — a seeded
  RNG, so a seed replays — whose events and log lines have the real bridge's shape.
  A game moves one turn each time its events are read, so a test can stop it
  halfway. Set `unknown` / `unsupported` on it to refuse or flag cards.
  `tests/fixtures/forge_game.json` is a game the real bridge played, for the summary.
- Shared fixtures are plain functions:
  - `tests/factories.py`: processed Scryfall cards (`scryfall_card`,
    `stored_cards`, `constructed_cards`), `make_deck(owner, …)` /
    `make_constructed_deck(…)`, `legal_decklist()` and `legal_constructed_decklist()`;
  - `tests/cards.py`: the HTTP `Response` stub.
- `Deck.status` defaults to `ready`, so decks created directly in tests need no
  import.

| File | Covers |
|---|---|
| `test_decklist.py`, `test_cards.py`, `test_commander.py`, `test_constructed.py`, `test_formats.py` | Parsing, card classification, legality, the format registry |
| `test_scryfall.py`, `test_cache.py`, `test_load_oracle_cards.py` | Fetching, cache backends, bulk load |
| `test_forge.py` | The bridge client, decklist text and format names |
| `test_runner.py`, `test_tasks.py`, `test_summary.py` | Whole matches on the fake bridge, failures and stops, the Celery task and recovery; the summary |
| `test_export.py` | A match's log as JSON lines or Markdown, and the export commands |
| `test_metagame.py`, `test_metagame_sources.py` | Metagame decks, their sync and sources |
| `test_forge_bridge.py` | Opt-in (`-m forge`): a match on a real bridge, replayed from its seed |
| `test_decks_web.py`, `test_matches_web.py`, `test_home.py` | Views end to end through the Django test client |
| `test_stats.py`, `test_sidebar.py` | Records per deck and per opponent, what needs attention; the sidebar and its sections |
| `test_migrations.py` | The migrations are in step with the models |
| `test_assets.py` | Template classes and icons vs. the vendored assets (below) |

## Templates, styles and icons

The stylesheets are **written by hand** and the icon font is a **prebuilt subset**,
both committed to the repo: there is no CSS build and no font subsetting step here.
`theme/static/css/` holds three files, linked in this order by
`partials/stylesheets.html`:

- `styles.css`: the tokens (colors, fonts, radii as custom properties in `:root`), the reset and the base typography;
- `layout.css`: the app shell (sidebar, main column), page furniture, the login page and their responsive rules;
- `components.css`: everything else, with the components' responsive rules last so they win.

A rule overriding another file's class must live in a later file. `tests/test_assets.py` guards what that implies:

- **Classes**: every `class="…"` token in a template must be defined in one of
  the stylesheets; a class with no rule does nothing. Add the rule to the right
  file (reusing the tokens) when a template needs a new class.
  `KNOWN_UNCOMPILED` stays empty. Build class names whole, never by concatenation
  (`card-face--{{ n }}` hides the class from the check).
- **Icons**: icons are Material Symbols ligatures (`<span class="ms" …>name</span>`)
  from `theme/static/vendor/gfonts/woff2/materialsymbols-subset.woff2`. The subset
  lacks the glyphs `j` and `q`, so names such as `query_stats` render as text. Check
  a new icon in the browser too.
- **Translation**: every icon span needs `translate="no"`, or a browser page
  translator rewrites the ligature name. Page titles and proper names (decks,
  cards, the app) carry it too.

Fonts are vendored under `theme/static/vendor/` (no CDN). Card images are the only
remote assets, loaded from `cards.scryfall.io`.

## Migrations

```bash
uv run python manage.py makemigrations
uv run python manage.py makemigrations --check --dry-run   # nothing pending?
uv run python manage.py migrate
```

Without a reachable Postgres, `makemigrations` still works but warns that it could
not run its consistency check; prefix it with `DATABASE_URL=sqlite:///:memory:` to
silence that.

Things that produce a migration without looking like a schema change:

- adding a format to `decks/formats.py` (`Deck.format` and `Match.format` choices —
  see [Deck import](deck-import.md#adding-a-format));
- adding a `Deck.Status` or a `Match.Status` value. (Forge's end
  reasons are stored verbatim; `END_REASONS` in `playtest/models.py` only names
  them.)

Changing the shape of the stored card data (`Deck.cards[*]["data"]`) needs a data
migration for decks already imported, with a test in `tests/test_migrations.py`.

## Logging

Console logging is set up in `settings.LOGGING` for the `mtg_gauntlet`, `decks`
and `playtest` loggers at `LOG_LEVEL` (default `INFO`). Lines from a background job
carry a `[deck <id>]` / `[match <id>]` prefix. Use `logging.getLogger(__name__)` in
new modules of these packages.
