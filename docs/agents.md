# Agents

An agent is whatever plays a seat. It never decides what is legal: Forge lists the
options of each decision (see [Forge](forge.md)), and the agent picks one of
them by index, or delegates the decision to Forge's AI.

**Every seat of a new match is Jev** (`playtest.forms.SEAT_AGENT`): the new-playtest form
offers decks, not agents or models, and ignores any posted agent or model: every seat
uses `~typesafe/jev-latest` (`typesafe.MODEL`). Jev is the only agent; `forge_ai` survives for older seats.

| `Seat.Agent` | Class | Network | Notes |
|---|---|---|---|
| `jev` | `JevAgent` | OpenRouter | Every new seat. One Jev `choice` per decision; logs its confidence. |
| `forge_ai` | — | none | Older matches only. Such a seat is created with `controller: "forge_ai"` and never reaches a worker. |

## The interface

`playtest/agents/base.py`:

```python
@dataclass(frozen=True)
class Decision:
    choice: int | None          # the picked option's index; None delegates to Forge's AI
    reasoning: str = ""         # why, in the agent's own words
    fallback_reason: str = ""   # set when it delegates: why, in words
    fallback_kind: str = ""     # "error" or "budget" ("low_confidence" on matches before 2026-10-18)
    confidence: float | None = None                    # Jev
    probabilities: tuple[tuple[str, float], ...] = ()  # top options by label
    scores: tuple[tuple[str, float], ...] = ()         # on a simulated decision: each outcome's score, by label
    model: str = ""             # the versioned model id that answered
    failed_play: str = ""       # the play Forge did not carry out just before (#71)
    trace: dict | None = None   # what the agent saw and answered; never sent to the bridge
    trace_id: int | None = None # the stored DecisionTrace, sent as meta["trace"]

    delegate: bool              # property: choice is None
    def meta(self) -> dict: ... # what the bridge stores on the event

def delegate(kind, reason, **extra) -> Decision: ...

class Agent(Protocol):
    def choose(self, view: dict, options: list[dict], *, kind: str = "", prompt: str = "",
               simulated: bool = False) -> Decision: ...
```

- `view`, `options` (`index`, `label`, `facts` and, on a simulated decision, `outcome`),
  `kind`, `prompt` and `simulated` are the bridge's decision as is. The bridge never asks
  when there is a single option.
- The worker answers `{"choice": i, "meta": ...}` or `{"delegate": true, "meta": ...}`;
  `meta` carries `confidence`, `probabilities`, `scores`, `model`, `reason`,
  `fallback_reason` and `fallback_kind`, and lands on the bridge's `decision` or
  `fallback` event, which the match page and the summary read. On a scored decision the
  log shows the pick's score and what the `choice` leaned to; the summary's "low"
  confidence counts the decisions answered under `LOW_CONFIDENCE` (0.2).

## Jev agent

`playtest/agents/jev_agent.py` — `JevAgent(model=None, *, seed=None, ask=None)`.

One Jev request per decision:

- **state** (`jev_state`): the view filtered to what a decision needs — turn, phase,
  active player; your life, hand with card text, library count, mana available,
  lands played, commanders with their tax; every battlefield with P/T, tapped,
  summoning sick, damage, counters and what an aura is attached to; opponents' hand
  and library counts; the stack and combat. Graveyards and exile are counts unless
  the decision is about them (a `choose_cards` or `target` prompt naming a graveyard
  or exile). No ids, no history.
- **one `choice` named `action`**: its instructions name the seat, the format, the
  turn, whose turn and phase it is and the goal, plus a line per decision kind
  (`KIND_LINES`), a line saying what passing means where the game stands on a priority
  decision (`step_line`: a non-empty stack or the own step; nothing on the opponent's turn, where a
  line telling Jev to pass unless answering made it passive with instants —
  upkeep and draw move on to the main phase, the second main phase ends the turn with
  its mana), a line for prompts whose rules text hides the stake (`prompt_line`: "No"
  to the command zone leaves the commander in the graveyard or exile), and Forge's
  prompt (#79). Its options are exactly the bridge's, keyed
  `o<index>`, each described by its label **plus Forge's facts** (`describe_option`);
  the model computes nothing.
- the options are **shuffled with the agent's seeded RNG** before each request and
  mapped back by key, because Jev leans toward the first option and Forge's first is
  always "Pass" or "Done".

**Simulated decisions.** When the bridge's decision carries `simulated: true` and at least
one option has an `outcome` (a whole attack, block or play, with the position Forge reached
on a copy of the game, see [Forge](forge.md)), the same request also asks one `score`
question per outcome ("How good is your position after action o3?", on `SCORE_LEVELS`: seven
generic levels from `lost` to `won`, score 0–6) and the state gains `candidates` (each
option's description and `outcome`). The pick is the best-scored option; options within
`TIE` (0.1) of the best go to the `choice`'s probability, and an option without an outcome
wins only when the `choice` gives it more than a half. The scores travel on the event
(`meta.scores`) and the trace (`scores`). Without outcomes the decision is a plain `choice`.

**No confidence gate.** Jev always plays its own pick: a low confidence is recorded, not
handed over (the gate measured on 2026-10-17 delegated 22% of the decisions to Forge's AI,
which the benchmark then measured instead of Jev). A missing key, a transport error, a
malformed answer or one naming no offered option delegates with `error`.

**Metering** happens in the worker (`playtest.workers.decide`) for agents marked
`metered` (Jev): past `MAX_AGENT_CALLS` (400) for the seat or
`MAX_MATCH_SECONDS` (1800) for the match, the seat delegates without calling the
model (`budget`).

## Decision traces

Every decision Jev is asked — answered, handed over under the confidence gate, or
failed because Jev could not be reached — is stored as a `DecisionTrace`
(`playtest/models.py`, table `decision_traces`), so a choice can be reviewed the way
Jev saw it. Decisions Jev is never asked (the call budget, a priority decision left
with "Pass priority" alone, more than 255 options) have none.

| Field | What |
|---|---|
| `match`, `seat_position`, `event` | the match (deleting it deletes its traces), the seat, and the `decision` or `fallback` event of the answer |
| `decision_id`, `turn`, `step`, `kind`, `prompt` | the bridge's decision |
| `instructions`, `state` | the question's instructions and the `jev_state` JSON, exactly as sent |
| `options` | `[{index, label, facts}]` in the order Jev was shown them (after the shuffle) |
| `probabilities` | Jev's full distribution, `{index: p}` |
| `scores` | on a simulated decision, Jev's score (0–6) of each option's outcome, `{index: score}`; each option's `outcome` is in `options` |
| `confidence`, `choice`, `model`, `latency_ms` | the answer; `choice` is null when Forge's AI decided |
| `fallback_kind`, `fallback_reason`, `failed_play` | as on the event |
| `outcome` | `done`, `no_effect` (the same decision came back: Forge did not carry the play out) or `delegated` (Forge's AI decided, also when Jev's pick never reached the game: the bridge timed out or the game had ended) |
| `flags` | the checks' findings (below) |
| `forge_ai_choice` | the option Forge's AI would have picked, when the bridge says |

`JevAgent.choose` puts what it sent and got back in `Decision.trace`; the worker
(`playtest.workers.decide`) stores the row before answering and sends only its id,
as `meta.trace`, which the runner uses to link the trace to its event. The worker
marks a trace `no_effect` when it sees the same priority decision come back; when
the match ends (or fails) the runner settles the others as `done` or `delegated`.
An attack or block declaration is asked one creature at a time; when a later step of it
delegates, the bridge undoes the earlier picks and Forge's AI declares the whole combat,
so those picks are settled as `delegated` too.
A pick the bridge no longer accepted is marked `delegated` and never checked. A trace
that cannot be stored is logged and skipped, and a failing analysis is logged: neither
ever costs the decision or the match's result.
A trace takes a few KB. Matches played before traces existed have none.

**Forge AI as a reference (#75).** When the bridge says which option Forge's AI would
pick (`forge_ai_choice`, see [Forge](forge.md)), the trace keeps it, the summary reports
per seat how often Jev agreed (`seats[].agreement`: `rated`, `agree`, `by_kind`, shown as
"Agrees with Forge AI"), and the inspector offers a "Disagrees with Forge AI" filter. It
is a reference, not a judge: Forge's AI is not assumed right.

**Exporting.** `manage.py export_match <id> [--format jsonl|md] [--seat N] [--flagged]
[--output PATH]` writes a match (to stdout by default): `jsonl`, one line per event with
each decision's trace; `md`, a narration per turn with each decision's compact state,
options and probabilities, choice, outcome and flags. `export_playtest <id> --output DIR`
writes one file per match (`<seed>-<opponent>.<ext>`). The logic is in
`playtest/export.py`; nothing is sent anywhere.

## Decision checks

When a match ends, `playtest/checks.py` runs deterministic checks over its traces —
pure functions, no model, like the summary — and flags the decisions that deserve a
look. Only decisions the agent took itself are checked. The checks read the bridge's
option labels and Forge's facts; they never compute a rule.

| Check | Flags |
|---|---|
| `no_effect` | a play Forge did not carry out (the trace's outcome) |
| `pass_with_plays` | "Pass priority" in your own second main phase, empty stack, with a `Cast …` option offered for a card that is not an instant and has no flash (holding those for the opponent's turn is a plan) |
| `land_not_played` | the last "Pass priority" of your turn in a main phase, empty stack, no land played this turn, a `Play …` option offered, and no land played later that turn |
| `ignored_lethal` | "Done" or "No attack" while an attack is `lethal if unblocked`; "Done" or "No block" while the prompt's unblocked damage is at least your life |
| `bad_attack` | an attack (or a whole plan) every attacker of which `dies if blocked by: …`, none being lethal |
| `mulligan_keep` | keeping seven cards with at most 1 or at least 6 lands (`MULLIGAN_LANDS` per format, `DEFAULT_MULLIGAN_LANDS = (1, 6)`) |
| `worse_outcome` | on a simulated decision, another option's outcome is at least as good on every count (your life, the opponents' life lost, your creatures kept, theirs killed, your cards in hand) and better on one: dominance, not a valuation |

A flag means "look at this", not "this was wrong": the bridge says which blockers kill
an attacker, not whether they die too, so `bad_attack` cannot tell a bad attack from
an even trade. Two known false positives wait for facts from the bridge
(mtg_forge_engine#8): `ignored_lethal` on a block when a trampler makes every block
hopeless, and `bad_attack` when an attack trigger stops all blockers (Aragorn as the
monarch). Each flag is stored on the trace (`flags: [{check, reason}]`), counted
per seat in `Match.summary` (`seats[].flags`, absent for a seat without traces, shown as
"Flagged decisions" on the match page), and summed for the deck under test on the
playtest page.

## Benchmark

To measure a change to Jev, its instructions or the bridge (#79), play the same games
before and after. `manage.py benchmark <match id> [--games 20] [--seed N]` copies the
match's two decks into two playtest sessions, one per deck in Jev's hands: Jev plays
seat 1 and Forge's AI the other deck in seat 2, on seeds `N … N + games - 1` in both
sessions, queued to the worker game by game. Reuse the seed to compare two runs.
`--control` plays the same games with Forge's AI in both seats (no Jev, no API key):
the record the matchup makes on its own, to read Jev's win rate against.

`manage.py benchmark_report <playtest id> …` prints, one column per session plus a
total, Jev's numbers over the finished matches, read from their summaries
(`playtest/benchmark.py`, no model): won–lost–draw and win rate, average turns, the
share of decisions Jev took itself and the fallbacks by kind, its median confidence,
the checks' flags per game and by check, and its agreement with Forge's AI by decision
kind (only with a bridge that reports `forge_ai_choice`).

## Jev client

`playtest/agents/typesafe.py` — `system_one(state, questions, *, model=None, key=None, sleep=time.sleep)`
against OpenRouter's `POST /api/v1/systemone`, which serves Jev in TypeSafe's native
`state` / `questions` format. No SDK.

| | |
|---|---|
| API key | `OPENROUTER_API_KEY` (`api_key()`) |
| Model | always `~typesafe/jev-latest`; the response's versioned id is returned |
| Retries | up to 4 attempts on `429` / `529`, honouring `retry-after`, exponential back-off |
| Timeout | `REQUEST_TIMEOUT = 30` seconds per request |
| Limits | `choice()` refuses more than 255 options |

`read_choice(answer, options)` checks an answer names an offered option. Every
failure is a `TypeSafeError`.

Without `OPENROUTER_API_KEY` the new-playtest form refuses to start: Jev plays every seat.

## Adding an agent

1. Write a class with `choose(view, options, *, kind="", prompt="") -> Decision`
   under `playtest/agents/`. Return an offered option's index, or `delegate(...)`
   with a reason. Set `metered = True` if it calls a model.
2. Add a value to `playtest.models.Seat.Agent` (a migration) and an icon in
   `Seat.agent_icon`.
3. Build it in `playtest.workers.build_agent` from the `Seat` and the match seed.
4. To let new matches use it, change `SEAT_AGENT` in `playtest/forms.py` (today every
   seat is Jev and the form offers no choice).
5. Test it without network: pass a fake `ask`, or monkeypatch
   `typesafe.system_one` / `requests.post`, and play it against the `fake_forge`
   fixture (`tests/test_jev_agent.py`, `tests/test_runner.py`).
