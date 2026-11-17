# MTG Gauntlet

A **Django** web app where **Forge's AI playtests competitive Magic: The Gathering decks**:
Standard, Pioneer, Modern, Legacy, Vintage, Pauper and Duel Commander (Commander decks,
cEDH included, import and check too). Import your decks from a plain-text list (cards
resolved through the **Scryfall** API), seat them at a table — or pit them against the
top decks of the format — and let Forge play the games out under the real rules.
Pages are **HTMX** over a precompiled stylesheet, data lives in **Postgres**.

The rules are **Forge**'s: [Forge](https://github.com/Card-Forge/forge), an open-source
rules engine with nearly every card scripted, plays every game from its own container
([`mtg_forge_engine`](https://github.com/gab-25/mtg_forge_engine), the *bridge*), and
Forge's own AI plays every seat. The deck under test always faces an opponent played
the same way; the user picks the decks, and the app follows each game, stores its log
and sums it up.

## Features

- **Deck import**: paste a Moxfield-style list (`4 Lightning Bolt`; a commander under a
  `Commander` header or tagged `*CMDR*`; a sideboard under a `Sideboard` header).
  Construction rules that plain text can settle are checked on the spot; the cards are
  then fetched from Scryfall in the background and checked again — copy limits,
  commander eligibility and color identity, and the ban list of the deck's format.
  Every problem is reported at once.
- **Formats**: a deck declares its format, and is checked against that format's
  construction rules and ban list:

  | Format | Players | Life | Deck | Copies | Commander |
  |---|---|---|---|---|---|
  | Standard, Pioneer, Modern, Legacy, Pauper | 2 | 20 | 60+ main, ≤15 sideboard | 4 | — |
  | Vintage | 2 | 20 | 60+ main, ≤15 sideboard | 4 (restricted: 1) | — |
  | Commander (cEDH included) | 2–4 | 40 | exactly 100 | 1 | yes, 21 damage |
  | Duel Commander | 2 | 20 | exactly 100 | 1 | yes |

  A constructed deck lists its sideboard under a `Sideboard` header; the sideboard is
  imported and checked, but a match is a single game played with the main deck.
- **Forge's AI on every seat**: free and at the engine's speed, with no API key. The
  deck under test and its opponents are always played the same way.
- **Coverage**: Forge either knows a card or the match is refused, naming the card.
  Each seat lists the cards Forge knows but cannot play fully. The deck page checks a
  deck on demand (**Check with Forge**), never on page view, and keeps the answer:
  the deck's badge, the home page and the new-playtest form show it.
- **Metagame decks**: `manage.py sync_metagame` imports the top decks of every format
  (EDHTop16 for Commander, MTGTop8 for the rest) as shared opponents, shown on the
  *Metagame* page and offered as opponents in the new-playtest form with their meta share; a deck with
  cards Forge does not know is flagged.
- **Partner commanders**: Partner, Partner with, Friends forever and Backgrounds are
  accepted at import and sent to Forge as the deck's commanders.
- **Playtests**: pick the deck to test, a list of opponents of its format (your
  decks and the metagame's, up to 10) and how many games it plays against each (1 to
  20, at most 50 in a session): every game runs in the background, an independent
  game 1 whose seed also decides, through Forge's coin toss, who plays first. The
  playtest page shows each opponent's record (W–L–D and win rate over finished
  games, with the games still playing, queued or failed) and every game's result,
  turns and life as they end, with a link to every match and **Run again**.
- **Around the deck**: a sidebar (Home · Decks · Playtests · Metagame) on every page.
  Home shows what is running, each deck's record and what needs attention (decks not
  checked with Forge, imports, a stale metagame); a deck's page adds its record
  against each opponent and its playtest history; the Metagame page has a tab per
  format with **Test my deck against these**.
- **Live game log and summary**: each match's page shows the table and Forge's log as
  it grows (HTMX polling), and at the end a deterministic summary per seat: result,
  life over time, turns, spells cast and lands played. A running match, or a whole
  playtest, can be stopped.
- **Reproducible**: a match has a seed, and the same seed replays the same game.
- **Scryfall cache in the database**: card JSON is cached in Postgres
  (`scryfall_cards`) and shared by every deck. Card images are not stored: the
  browser loads them straight from Scryfall's CDN (`cards.scryfall.io`). The card table can be
  filled in one go from Scryfall's bulk data (see [Local card database](#local-card-database)).
- **Ownership**: decks and matches belong to the user who created them; anyone
  else gets a 404.

## Rules

- **Forge enforces the comprehensive rules** and every card's own script: turns and
  priority, the stack, mana, combat, state-based actions, layers, replacement effects.
- **Playable formats**: Duel Commander and the six constructed formats, two seats.
  Multiplayer Commander decks import and can be checked against Forge, but cannot be
  played until the bridge seats more than two players.
- **Forge's AI plays**: every decision of every seat — mulligans, spells, attacks,
  blocks, mana payment — is Forge's AI's.
- **The end**: a winner, or a draw at the bridge's turn cap (`MAX_TURNS`).

See [Forge](docs/forge.md) for the bridge's contract.

## Documentation

- [Architecture](docs/architecture.md) — how the apps fit together, the request →
  background job → stored result flow, the data model and the boundaries.
- [Forge](docs/forge.md) — the bridge's contract, the client, the runner,
  reproducibility, the summary and the match export.
- [Deck import](docs/deck-import.md) — parsing (sideboards included), Scryfall
  fetching, the card cache, legality checks per format family and the format
  registry.
- [Development](docs/development.md) — running locally and with Docker, tests,
  template/asset constraints, migrations.

## Project structure

```
manage.py
mtg_gauntlet/        # Django project: settings, URLs, layout templates, home page
├── jobs.py             #   Background jobs (thread per job; inline in tests)
├── access.py           #   Ownership check shared by every view
└── logging_context.py  #   Stamps "[match <id>]" / "[deck <id>]" on job logs
decks/                  # App: decks and the Scryfall cache
├── rules/              #   Decklist parsing, card types, Commander and constructed legality (pure)
├── formats.py          #   Every format's parameters, family and name on the Forge bridge
├── scryfall.py         #   Card data fetching
├── cache.py            #   Database and filesystem cache backends
├── metagame/           #   Metagame sources and sync
└── importer.py         #   parse → fetch → validate, and the import job
playtest/               # App: playtest sessions and their matches
├── forge.py            #   HTTP client of the Forge bridge
├── runner.py           #   Starts a game on the bridge, stores its events and result
├── tasks.py            #   The Celery task following a match, and the worker's recovery
├── summary.py          #   The deterministic match summary
├── export.py           #   A match as JSON lines or Markdown, for offline review
└── forms.py            #   The new-playtest form: a deck to test and its opponents
theme/static/           # Precompiled stylesheet, vendored fonts and icons
tests/                  # Hermetic suite; fakeforge.py is an in-memory bridge
docs/                   # Project documentation (see Documentation above)
CLAUDE.md               # Notes for Claude Code
```

## Running it

The project uses [`uv`](https://docs.astral.sh/uv/).

### Docker Compose

```bash
docker compose up --build
docker compose exec web python manage.py createsuperuser
```

This also pulls and starts the Forge bridge (`forge`, the
`ghcr.io/gab-25/mtg_forge_engine` image), Redis (`redis`) and the Celery worker that
plays the matches (`worker`); `web` waits for the bridge's health check, about a
minute while the card database loads. Then open <http://localhost:8000>.

### Locally

```bash
uv sync
cp .env.example .env                  # then edit it
docker compose up -d forge            # the Forge bridge on localhost:8080 (FORGE_URL's default)
docker compose up -d redis            # the Celery broker on localhost:6379
uv run python manage.py migrate
uv run python manage.py createsuperuser
uv run python manage.py runserver     # http://localhost:8000
uv run celery -A mtg_gauntlet worker --pool threads -l info   # in a second terminal: plays the matches
```

## Local card database

Load every card from Scryfall's [Oracle Cards](https://scryfall.com/docs/api/bulk-data)
bulk file into `scryfall_cards`, so importing a deck resolves its cards locally instead
of calling the Scryfall API once per card:

```bash
uv run python manage.py load_oracle_cards                  # download (~25 MB gzipped) and load
uv run python manage.py load_oracle_cards --if-newer       # skip if already up to date
uv run python manage.py load_oracle_cards --file oracle-cards.jsonl.gz   # offline, local file (JSONL, gzipped or not)
```

With Docker: `docker compose exec web python manage.py load_oracle_cards`.

A second run updates the cards it already has. Cards missing from the table (spoiled
after the last load, a typo) still go through the Scryfall API.

Scryfall regenerates the file daily, and the prices it carries are those of the download
day. Run the command **daily or weekly** with `--if-newer` to keep cards and prices
current, e.g. from cron:

```cron
30 4 * * * cd /path/to/mtg_gauntlet && uv run python manage.py load_oracle_cards --if-newer
```

## Metagame decks

Import the current top decks of each format as opponents:

```bash
uv run python manage.py sync_metagame                     # every format, 10 decks each
uv run python manage.py sync_metagame --format modern --limit 5
```

Commander comes from [EDHTop16](https://edhtop16.com), Duel Commander and the
constructed formats from [MTGTop8](https://www.mtgtop8.com). Each run replaces that
format's decks (a source that fails keeps the previous ones), imports them with the
same rules as your decks, and, when the Forge bridge runs, checks each one for cards
Forge does not know. Load the local card database first so the import needs few
Scryfall calls. Run it weekly, e.g. from cron, after `load_oracle_cards`.

## Exporting a match

Export a match's log, or every match of a playtest, to review it offline:

```bash
uv run python manage.py export_match <match id>                        # JSON lines, to stdout
uv run python manage.py export_match <match id> --format md --seat 1 --output match.md
uv run python manage.py export_playtest <playtest id> --format md --output exports/
```

`jsonl` is one line per event; `md` narrates each turn of Forge's log.
The commands only write files: nothing is sent anywhere.

## Configuration

Everything is configured through environment variables (a `.env` file is loaded in
local development; real environment variables win):

| Variable | Meaning | Default |
|---|---|---|
| `DATABASE_URL` | Postgres or SQLite URL | `postgresql://mtg:mtg@localhost:5432/mtg_tester` |
| `FORGE_URL` | The Forge bridge (Compose sets `http://forge:8080`) | `http://localhost:8080` |
| `CELERY_BROKER_URL` | Redis the matches are queued on (Compose sets `redis://redis:6379/0`) | `redis://localhost:6379/0` |
| `CELERY_CONCURRENCY` | Compose only: matches the worker follows at once | `16` |
| `SECRET_KEY` / `DEBUG` | Django basics | dev key / on |
| `CSRF_TRUSTED_ORIGINS` | Comma-separated origins behind a proxy | — |
| `RUN_JOBS_IN_BACKGROUND` | Run deck imports off the request thread | `1` |
| `LOG_LEVEL` | Console log level | `INFO` |

The bridge has its own settings — `MAX_TURNS` (the turn cap) and
`MAX_CONCURRENT_GAMES` among them — described in
[its README](https://github.com/gab-25/mtg_forge_engine#run); Compose passes those two
through.

## Tests

```bash
uv run pytest
FORGE_URL=http://localhost:8080 uv run pytest -m forge   # opt-in: against a real bridge
```

The suite is hermetic: in-memory SQLite, no Scryfall or Forge calls (a guard fails
any test that reaches the network). Matches play against
`tests/fakeforge.py`, an in-memory bridge; only the tests marked `forge` reach a real
one, and they skip without `FORGE_URL`. It also checks that templates only use classes
the hand-written stylesheet defines and icons the vendored font subset can render.

## License

GPL-3.0 — see [LICENSE](LICENSE).
