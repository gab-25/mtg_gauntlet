# MTG Gauntlet

A **Django** web app where **AI agents playtest competitive Magic: The Gathering decks**:
Standard, Pioneer, Modern, Legacy, Vintage, Pauper and Duel Commander (Commander decks,
cEDH included, import and check too). Import your decks from a plain-text list (cards
resolved through the **Scryfall** API), seat them at a table — or pit them against the
top decks of the format — and let the agents play the game out under the real rules.
Pages are **HTMX** over a precompiled stylesheet, data lives in **Postgres**.

The rules are **Forge**'s: [Forge](https://github.com/Card-Forge/forge), an open-source
rules engine with nearly every card scripted, plays every game from its own container
([`mtg_forge_engine`](https://github.com/gab-25/mtg_forge_engine), the *bridge*) and asks
each seat one decision at a time. The seats are played by
[Jev](https://docs.typesafe.ai/introduction), TypeSafe AI's System One model, which picks
among Forge's options and says how sure it is. Jev plays both seats: the deck under test
always faces another Jev agent, and the user picks decks, never agents. No model is
ever the rules engine: Forge lists the legal options; Jev only picks one of them, or
hands the decision back to Forge's AI.

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
- **Jev on every seat**: Jev picks each of Forge's options through OpenRouter, and its
  confidence and runner-up options are logged. There is no agent to choose: both seats
  are Jev, always on `~typesafe/jev-latest`. Forge's own AI never plays a seat;
  it only takes fallbacks and what the bridge does not expose. A seat that cannot reach
  Jev hands that decision to Forge's AI, with the reason logged, so a match never stalls
  on a model.
- **Simulated outcomes**: attacks, blocks and plays are whole actions, each with the
  position Forge reaches when it is carried out on a copy of the game (the opponent not
  responding); Jev rates each position on a fixed scale from "lost" to "won" and plays
  the best one. The inspector and the exports show each option's outcome and score.
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
- **Live game log and summary**: each match's page shows the
  table and Forge's log as it grows (HTMX polling), each Jev decision with its
  confidence, each fallback with its reason, and at the end a deterministic summary per
  seat: result, life over time, turns, spells cast, lands played, the agent's decisions
  against Forge AI's, confidence and fallbacks by reason, and the decisions the
  deterministic checks flagged. A running match, or a whole playtest, can be stopped.
- **Decision inspector**: the match page's **Decisions** tab lists every decision Jev
  was asked (filter by seat, kind, Forge AI fallbacks, flagged or no-effect plays); a
  decision's page shows the board and hand as Jev saw them, Forge's prompt, every
  option with its facts and Jev's probability, the choice, the outcome and the flags.
- **Reproducible**: a match has a seed. The same seed and the same answers replay the
  same game, so a match is as reproducible as Jev.
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
- **Always Forge's AI**: mana payment, combat damage assignment and the other
  decisions the bridge does not expose (scry, coin flips, …) are decided by Forge's AI
  for every seat, and logged as such.
- **The end**: a winner, or a draw at the bridge's turn cap (`MAX_TURNS`).

See [Forge](docs/forge.md) for the decision protocol.

## Documentation

- [Architecture](docs/architecture.md) — how the apps fit together, the request →
  background job → stored result flow, the data model and the boundaries.
- [Forge](docs/forge.md) — the bridge's contract, the client, the runner, the
  worker pool, reproducibility and the summary.
- [Agents](docs/agents.md) — the agent interface, the Jev agent, the
  Jev and OpenRouter clients and the fallbacks to Forge's AI.
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
playtest/               # App: matches between agents
├── forge.py            #   HTTP client of the Forge bridge
├── runner.py           #   Starts a game on the bridge, stores its events and result
├── workers.py          #   The worker pool answering Forge's decisions
├── agents/             #   the Jev agent and its client
├── summary.py          #   The deterministic match summary
├── checks.py           #   Deterministic checks that flag suspicious decisions
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
export OPENROUTER_API_KEY="..."       # required to start a match: Jev plays every seat
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

Every decision Jev is asked is stored with what it saw (state, Forge's prompt, every
option with its facts, in the order shown) and its full distribution. Export a match,
or every match of a playtest, to review it offline, by hand or with a model:

```bash
uv run python manage.py export_match <match id>                        # JSON lines, to stdout
uv run python manage.py export_match <match id> --format md --seat 1 --flagged --output match.md
uv run python manage.py export_playtest <playtest id> --format md --output exports/
```

`jsonl` is one line per event, each decision with its trace; `md` narrates each turn.
The commands only write files: nothing is sent anywhere.

## Benchmarking Jev

To tell whether a change makes Jev play better, replay the same games before and after:

```bash
uv run python manage.py benchmark <match id> --games 20 --seed 1000   # Jev vs Forge's AI, each deck
uv run python manage.py benchmark <match id> --games 20 --seed 1000 --control  # the same games, Forge's AI only
uv run python manage.py benchmark_report <playtest id> <playtest id>   # once the worker played them
```

The report compares Jev's record, how much of the game it played itself, its
confidence, the flagged decisions and its agreement with Forge's AI
([agents](docs/agents.md#benchmark)).

## Configuration

Everything is configured through environment variables (a `.env` file is loaded in
local development; real environment variables win):

| Variable | Meaning | Default |
|---|---|---|
| `DATABASE_URL` | Postgres or SQLite URL | `postgresql://mtg:mtg@localhost:5432/mtg_tester` |
| `FORGE_URL` | The Forge bridge (Compose sets `http://forge:8080`) | `http://localhost:8080` |
| `CELERY_BROKER_URL` | Redis the matches are queued on (Compose sets `redis://redis:6379/0`) | `redis://localhost:6379/0` |
| `CELERY_CONCURRENCY` | Compose only: matches the worker follows at once | `16` |
| `FORGE_WORKERS` | Worker threads answering Forge's decisions: the most Jev calls in flight; `0` lets each match's runner answer its own | `4` |
| `OPENROUTER_API_KEY` | Jev's key: required to start a match | — |
| `MAX_AGENT_CALLS` | Model calls per seat; past it the seat delegates to Forge's AI | `400` |
| `MAX_MATCH_SECONDS` | Match wall clock; past it model seats delegate to Forge's AI | `1800` |
| `SECRET_KEY` / `DEBUG` | Django basics | dev key / on |
| `CSRF_TRUSTED_ORIGINS` | Comma-separated origins behind a proxy | — |
| `RUN_JOBS_IN_BACKGROUND` | Run deck imports off the request thread | `1` |
| `LOG_LEVEL` | Console log level | `INFO` |

The bridge has its own settings — `DECISION_TIMEOUT` (an unanswered decision goes to
Forge's AI), `MAX_TURNS` (the turn cap), `MAX_CONCURRENT_GAMES` and `ORPHAN_TIMEOUT` —
described in [its README](https://github.com/gab-25/mtg_forge_engine#run); Compose
passes the first three through.

Jev is reached through OpenRouter (`/api/v1/systemone`), so one key covers everything;
no TypeSafe account is needed. A seat asks Jev once per decision Forge
exposes (a single-option decision is never asked), and a match can take a few hundred
decisions: keep an eye on your OpenRouter spend.

## Tests

```bash
uv run pytest
FORGE_URL=http://localhost:8080 uv run pytest -m forge   # opt-in: against a real bridge
```

The suite is hermetic: in-memory SQLite, no Scryfall, OpenRouter or Forge calls (a
guard fails any test that reaches the network), and no API key. Matches play against
`tests/fakeforge.py`, an in-memory bridge; only the tests marked `forge` reach a real
one, and they skip without `FORGE_URL`. It also checks that templates only use classes
the hand-written stylesheet defines and icons the vendored font subset can render.

## License

GPL-3.0 — see [LICENSE](LICENSE).
