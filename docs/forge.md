# Forge

The rules are not written here. [Forge](https://github.com/Card-Forge/forge), an
open-source rules engine with nearly every card scripted, plays every game. It runs in
its own container, [`mtg_forge_engine`](https://github.com/gab-25/mtg_forge_engine)
(the *bridge*), which exposes each player decision over HTTP. This app keeps the decks,
the agents, the match page and the summary.

No model is the rules engine, and neither is this app: Forge lists the legal options
of every decision; an agent only picks one of them, or hands the decision back to
Forge's AI.

## The bridge's contract

| Method | Path | Used for |
|---|---|---|
| `GET` | `/health` | `{"status": "ready", "cards": N}`; 503 while Forge loads its card database |
| `POST` | `/games` | `{"format", "seed", "seats": [{"name", "decklist", "controller"}] × 2}` → the game, with each seat's `unsupported_cards`. Unknown cards: 422 `{"detail": {"unresolved": {"<seat>": [...]}}}` |
| `POST` | `/decks/check` | `{"format", "decklist"}` → `{"unresolved", "unsupported_cards", "missing_commander"}`; no game starts |
| `GET` | `/games/{id}` | `status` (`queued`/`running`/`finished`/`failed`/`aborted`), `turn`, `winner` (a seat index), `end_reason`, `error` |
| `GET` | `/games/{id}/events?since=N` | `{"events": [...], "next": M}` |
| `DELETE` | `/games/{id}` | abort |
| `GET` | `/decisions?wait=30&game=<id>` | the oldest pending decision of any game (or of `game`); 204 if none; may carry `forge_ai_choice` |
| `POST` | `/decisions/{id}` | `{"choice": i}` or `{"delegate": true}`, with an optional `meta` stored on the event; 409 when the decision is no longer pending |

A seat's `controller` is `external` (a worker answers its decisions) or `forge_ai`
(Forge's AI plays it and it never reaches a worker). A playtest's player sets it for
every seat of its matches: `external` for Jev, `forge_ai` for Forge AI.

**Decisions.** A decision has a `kind` — `mulligan`, `bottom`, `priority`, `target`,
`attack`, `block`, `choose_cards`, `confirm`, `mode` — Forge's `prompt`, the seat's
filtered `view` (no hidden information) and up to 255 numbered `options`, each with a
`label` and code-computed `facts` ("deals 2 damage if unblocked", "dies if blocked by:
…"). Multi-selects (attackers, blockers, cards) come one pick at a time, option 0 being
"Done". A decision also carries `forge_ai_choice`: the option Forge's AI would pick, or
`null` when it has no answer, computed without changing the game or drawing from its
seeded generator; in a multi-select it points at the AI's next pick, then at "Done"
([mtg_forge_engine#7](https://github.com/gab-25/mtg_forge_engine/pull/7)). The bridge
copies it into the `decision` and `fallback` events too. It is optional for the client:
the worker keeps it in the decision trace only when it is an offered index, and an older
bridge without it keeps working. **Everything else is always Forge's AI**: mana payment, combat damage
assignment, scry and the other choices the bridge does not expose; each is logged as a
`forge_ai` event.

**Events.** `{"seq", "turn", "phase", "seat", "kind", "text", "payload"}` with kinds:

- `log`: Forge's own game log; `payload.type` is its category (`TURN`, `PHASE`, `LIFE`,
  `STACK_ADD`, `STACK_RESOLVE`, `LAND`, `DAMAGE`, `COMBAT`, `MANA`, `ZONE_CHANGE`,
  `GAME_OUTCOME`, …);
- `decision`: a client's answer, with `kind`, `choice` and the answer's `meta`;
- `fallback`: Forge's AI decided instead; `reason` is `delegate` (the client asked,
  its `meta.fallback_kind` says why), `timeout`, `withdrawn`, `orphaned`,
  `too_many_options`, `invalid_attack`, `invalid_block` or `error`;
- `play_failed`: a spell or ability the seat chose that Forge did not play
  (`payload.ability`, `payload.reason`: `modes`, `targets` or `costs`); the same
  priority decision comes back;
- `forge_ai`: a decision the bridge never exposes (`payload.method`);
- `game_over`: `winner` and `end_reason`.

**End reasons** are Forge's (`AllOpponentsLost`, `Draw`, `Conceded`, …) plus the
bridge's `turn_limit`, `aborted` and `orphaned`; `playtest.models.END_REASONS` names
them on the match page.

**Timeouts** (bridge settings, see its README): a leased decision not answered within
`DECISION_TIMEOUT` goes to Forge's AI; a game none of whose decisions is leased for
`ORPHAN_TIMEOUT` is aborted as `orphaned`; past `MAX_TURNS` the game is a draw
(`turn_limit`). A game therefore always ends, and never waits on a model.

## The client

`playtest/forge.py` is the only module that speaks HTTP to the bridge:

- `ForgeClient(url=None)` (default `FORGE_URL`, else `http://localhost:8080`) with
  `health`, `create_game`, `check_deck`, `game`, `events`, `lease`, `answer`, `abort`.
  `client()` returns one; tests monkeypatch it with a fake.
- Every failure is a `ForgeError`; unknown cards raise `ForgeDeckError`, whose
  `unresolved` maps a seat name to its cards. `answer` returns `False` on a 409.
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
   decklists, the seed and the controllers, and stores the bridge's game id and each
   seat's coverage (`{"unsupported_cards": [...]}`). `run_match` starts the other
   games when the worker picks them up. The bridge has no way to choose who plays
   first: Forge's coin toss draws from the game's seeded RNG, so each game's seed
   decides it, reproducibly, and games of one matchup land on the play or on the draw
   as the seeds fall. If Forge refuses any deck
   (`ForgeDeckError`) the games already started are aborted, the session is deleted
   and the form names every unknown card (422); on any other error the same with 503.
   A deck Forge cannot play never becomes a playtest.
2. **The worker follows it.** `runner.run_match` (the Celery task `run_match_task`,
   on the worker) marks the match
   running and copies the bridge's events into `MatchEvent` rows as they arrive,
   keeping `Match.turn` and each seat's life current. The bridge may be unreachable for up to a minute
   (`MAX_OUTAGE_SECONDS`) before the match fails; a deleted match or a game past
   `MATCH_TIMEOUT` (3 hours) is aborted. When the bridge no longer knows the game (it
   restarted) the match fails at once. A match that fails for any reason aborts its
   game, and workers delegate every decision of a match that no longer runs.
3. **When the game ends** it stores the winner, Forge's end reason, each seat's final
   life and the summary. Status: `finished`; `stopped` when aborted by the user;
   `failed` when Forge failed or the game was `orphaned`.

A running match can be stopped from its page (`matches/<id>/stop`), a whole session
from its own (`playtests/<id>/stop`), and deleting either aborts the games still
running. Stopping also marks the match `stopped` itself, so a match
still in the queue is left alone by its task.

## The worker pool

`playtest/workers.py`: `FORGE_WORKERS` threads (default 4) start once in the Celery
worker, on `worker_ready` (`playtest/tasks.py`), and stop on `worker_shutdown`; web
processes never run a pool. Each long-polls `lease(30)`, finds the `Match` by its
`forge_game_id` and the `Seat` by position, asks the seat's agent and answers. The
pool size bounds the model calls in flight across every running match.

- Agents live in an `AgentCache`, one per (match, seat), for the whole game: a game's
  decisions come one at a time, so a seat's seeded RNG (Jev's option order) is
  consumed in order.
- A decision for a game not yet linked to a match waits up to 5 seconds for the link
  (the bridge starts a game before the view stores its id), then is delegated.
- Model-backed agents are metered: past `MAX_AGENT_CALLS` (1000) for the seat or
  `MAX_MATCH_SECONDS` (1800) since the match started, the seat delegates every
  remaining decision (`fallback_kind = "budget"`).
- A play Forge does not carry out brings the very same priority decision back
  (same prompt, view and options); bridges before the stack-target fix offered every
  counterspell that targets the stack (Stifle, Spell Snare, Force Spike…), whose cast
  then aborted. The worker then leaves that option
  out of the decision, along with any other that failed in it, and the answer's
  `meta.failed_play` names the play; a decision left with "Pass priority" alone
  passes without asking the agent. So a seat never loops on a failed play (#71).
- Every decision the agent is asked is stored as a `DecisionTrace` before answering
  ([Agents](agents.md#decision-traces)); the answer's `meta.trace` is its id, which the
  runner uses to link it to the event.
- An agent's exception, a seat with no agent or an option out of range is delegated
  (`fallback_kind = "error"`); the worker keeps running. Bridge errors are logged and
  retried after a pause. If a worker dies anyway, `DECISION_TIMEOUT` hands its
  decision to Forge's AI: no match stalls.

With `FORGE_WORKERS=0` (and in tests, where tasks run eagerly) no pool runs: each
match's runner answers its own decisions between reads of the log.

**Two knobs.** `CELERY_CONCURRENCY` (default 16) is how many matches the worker
*follows* at once — threads copying the log, cheap. `FORGE_WORKERS` is how many Jev
calls are in flight, globally, since there is one worker process: keep the `worker`
service at one replica. The first game of each opponent starts on the bridge in the
view, so the queue does not bound those (the bridge's `MAX_CONCURRENT_GAMES` does): the
pool answers the decisions of a game whose follower is still queued, only its log is
copied later. A session's other games start only when their task runs, so the queue
does bound them.
Celery's time limits do not apply with the thread pool; `MATCH_TIMEOUT` and the
agents' budgets bound a match.

**Restarts.** Tasks are acknowledged on receipt. When the worker starts it re-queues
every `pending` or `running` match; a resumed match copies the log from its last
stored event, but its agents are new, so it no longer replays exactly from its seed.

## Reproducibility

The bridge replays a game from its seed when the answers are the same — every event
but Forge's wall-clock line ("Match Duration …"). Jev's option order is seeded too, so
Jev seats are as reproducible as Jev's answers: a stub that answers deterministically
(as in the tests) replays exactly. Computing `forge_ai_choice` updates the AI's own
memory for the seat, which only matters to decisions it then takes: a seed still replays,
but a game played on a bridge that computes it can differ from the same game played on
an older one.

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

From the bridge's own events it counts, per seat, the agent's decisions against
Forge AI's (fallbacks and `forge_ai` events), the median confidence, low-confidence
fallbacks and fallbacks by reason (`fallback_reason`: the agent's `fallback_kind`
when it delegated, else the bridge's reason) and the failed plays (`play_failed`
events, or with a bridge that does not report them the decisions carrying
`failed_play`, see the worker pool). A fallback is followed by a `forge_ai`
event for the very decision Forge's AI then takes; `fallback_executions` pairs the
two so the decision counts (and shows) once. `automatic` counts the non-exposed
decisions by Forge method. The match page hides `PHASE` and `MANA` log lines and
folds runs of `forge_ai` events into one line.

## Limitations

- Two seats per game: multiplayer Commander waits for a bridge extension.
- The bridge keeps no state across restarts: running matches then fail.
- One worker process: a second replica would run a second pool, so the bound on Jev
  calls doubles and a game's decisions may reach agents in different processes.
