# Forge

The rules are not written here. [Forge](https://github.com/Card-Forge/forge), an
open-source rules engine with nearly every card scripted, plays every game, and its
own AI plays every seat. It runs in its own container,
[`mtg_forge_engine`](https://github.com/gab-25/mtg_forge_engine) (the *bridge*). This
app keeps the decks, starts the games, follows them, and keeps the log and the summary.

Neither the app nor any model decides anything in a game: rules, card effects and
every play are Forge's.

## The bridge's contract

| Method | Path | Used for |
|---|---|---|
| `GET` | `/health` | `{"status": "ready", "cards": N}`; 503 while Forge loads its card database |
| `POST` | `/games` | `{"format", "seed", "seats": [{"name", "decklist"}] × 2}` → the game, with each seat's `unsupported_cards`. Unknown cards: 422 `{"detail": {"unresolved": {"<seat>": [...]}}}` |
| `POST` | `/decks/check` | `{"format", "decklist"}` → `{"unresolved", "unsupported_cards", "missing_commander"}`; no game starts |
| `GET` | `/games/{id}` | `status` (`queued`/`running`/`finished`/`failed`/`aborted`), `turn`, `winner` (a seat index), `end_reason`, `error` |
| `GET` | `/games/{id}/events?since=N` | `{"events": [...], "next": M}` |
| `DELETE` | `/games/{id}` | abort |

**Events.** `{"seq", "turn", "phase", "seat", "kind", "text", "payload"}` with kinds:

- `log`: Forge's own game log; `payload.type` is its category (`TURN`, `PHASE`, `LIFE`,
  `STACK_ADD`, `STACK_RESOLVE`, `LAND`, `DAMAGE`, `COMBAT`, `MANA`, `ZONE_CHANGE`,
  `GAME_OUTCOME`, …);
- `game_over`: `winner` and `end_reason`.

The bridge reads Forge's log when the game ends: while a game runs, `GET /games/{id}`
shows its status and turn, and the events come in one batch at the end.

**End reasons** are Forge's (`AllOpponentsLost`, `Draw`, `Conceded`, …) plus the
bridge's `turn_limit` and `aborted`; `playtest.models.END_REASONS` names them on the
match page. Past `MAX_TURNS` (a bridge setting, see its README) the game is a draw
(`turn_limit`), so a game always ends.

## The client

`playtest/forge.py` is the only module that speaks HTTP to the bridge:

- `ForgeClient(url=None)` (default `FORGE_URL`, else `http://localhost:8080`) with
  `health`, `create_game`, `check_deck`, `game`, `events`, `abort`. `client()` returns
  one; tests monkeypatch it with a fake.
- Every failure is a `ForgeError`; unknown cards raise `ForgeDeckError`, whose
  `unresolved` maps a seat name to its cards; a game the bridge no longer knows raises
  `ForgeGameNotFound`.
- `decklist_text(cards, has_commander=...)` turns stored `Deck.cards` into the text
  the bridge reads (`<qty> <name>`, the commander under `Commander`, the sideboard
  under `Sideboard`).
- `bridge_format(fmt)` reads `Format.forge_format` from `decks/formats.py`
  (`duel` → `duel_commander`, constructed formats by their key). Multiplayer
  Commander has none and is refused; `check_deck` checks a Commander deck as
  `duel_commander`, since Forge knows the same cards.

## A match, end to end

1. **The view starts the games.** The user picks a deck to test and up to ten
   opponents (`playtest.forms.MAX_OPPONENTS`), and 1 to 20 games against each
   (`MAX_GAMES_PER_OPPONENT`, at most `MAX_GAMES` = 50 in all): `create_playtest`
   stores a `Playtest` session and `games_per_opponent` `Match`es per opponent (the
   tested deck in seat 0, seeds `seed + opponent index × games + game index`), then
   calls `runner.start_game` for the first game of each opponent, which sends both
   decklists and the seed, and stores the bridge's game id and each seat's coverage
   (`{"unsupported_cards": [...]}`). `run_match` starts the other games when the
   worker picks them up. The bridge has no way to choose who plays first: Forge's coin
   toss draws from the game's seeded RNG, so each game's seed decides it,
   reproducibly. If Forge refuses any deck (`ForgeDeckError`) the games already
   started are aborted, the session is deleted and the form names every unknown card
   (422); on any other error the same with 503. A deck Forge cannot play never becomes
   a playtest.
2. **The worker follows it.** `runner.run_match` (the Celery task `run_match_task`,
   on the worker) marks the match running, polls the game and copies the bridge's
   events into `MatchEvent` rows as they arrive, keeping `Match.turn` and each seat's
   life current. The bridge may be unreachable for up to a minute
   (`MAX_OUTAGE_SECONDS`) before the match fails; a deleted match or a game past
   `MATCH_TIMEOUT` (3 hours) is aborted. When the bridge no longer knows the game (it
   restarted) the match fails at once. A match that fails for any reason aborts its
   game.
3. **When the game ends** it stores the winner, Forge's end reason, each seat's final
   life and the summary. Status: `finished`; `stopped` when aborted by the user;
   `failed` when Forge failed.

A running match can be stopped from its page (`matches/<id>/stop`), a whole session
from its own (`playtests/<id>/stop`), and deleting either aborts the games still
running. Stopping also marks the match `stopped` itself, so a match still in the queue
is left alone by its task.

**Concurrency.** `CELERY_CONCURRENCY` (default 16) is how many matches the worker
follows at once — threads polling the bridge, cheap. The bridge's
`MAX_CONCURRENT_GAMES` bounds the games Forge plays at once; the first game of each
opponent starts in the view, so the queue does not bound those. Keep the `worker`
service at one replica: `tasks._following` drops a duplicate delivery only within one
process. Celery's time limits do not apply with the thread pool; `MATCH_TIMEOUT`
bounds a match.

**Restarts.** Tasks are acknowledged on receipt. When the worker starts it re-queues
every `pending` or `running` match; a resumed match copies the log from its last
stored event.

## Reproducibility

The bridge replays a game from its seed — every event but Forge's wall-clock line
("Match Duration …").

## The summary

`playtest/summary.py::summarize_match(events, seats, fmt, game)` is pure and
deterministic. It reads Forge's log by the names the seats played under
(`table_names`: the deck's name, plus the seat when a deck sits twice):

| Log line | Category | Counts |
|---|---|---|
| `Turn 3 (Red)` | `TURN` | the turns each seat played |
| `Life: Green 20 > 17` | `LIFE` | life over time and final life |
| `Red cast Lightning Bolt targeting [Green]` | `STACK_ADD` | spells cast (`triggered` / `activated` lines are not spells) |
| `Red played Mountain (31)` | `LAND` | lands played |

The match page hides `PHASE` and `MANA` log lines.

## Exporting a match

`playtest/export.py` turns a match's events into text: `match_jsonl` (one JSON object
per event, in order) or `match_markdown` (Forge's log narrated per turn, without the
quiet categories). `seat` keeps one seat's events and the table-wide ones. The
`export_match` and `export_playtest` commands write them to stdout or files; nothing is
sent anywhere.

## Limitations

- Two seats per game: multiplayer Commander waits for a bridge extension.
- The bridge keeps no state across restarts: running matches then fail.
- The log arrives when the game ends: a running match's page shows its turn, not its
  log.
