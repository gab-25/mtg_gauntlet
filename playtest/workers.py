"""The worker pool: threads that answer Forge's decisions for every running match.

Each worker long-polls the bridge for the oldest pending decision of any game,
finds the match and the seat it belongs to, asks the seat's agent and answers.
``FORGE_WORKERS`` threads (default 4) start once, when the Celery worker is
ready (:mod:`.tasks`); there is one worker process, so their number bounds the
model calls in flight. With ``FORGE_WORKERS=0`` (and in tests) no pool runs and
each match's runner answers its own decisions, one at a time.

A worker never stops on an error: a decision it cannot serve is delegated to
Forge's AI, and if a worker dies anyway the bridge's ``DECISION_TIMEOUT`` hands
its decision to Forge's AI, so no match stalls.

A play Forge did not carry out (a cast aborted before reaching the stack) brings
the very same priority decision back: the worker leaves that option out of it,
so a seat never loops on it (#71).

Every decision the agent was asked is stored as a ``DecisionTrace`` before the
answer goes back; only its id travels, in ``meta.trace`` (#72).
"""

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, replace

from django.db import DatabaseError, close_old_connections
from django.utils import timezone

from . import forge
from .agents import typesafe
from .agents.base import Decision, delegate
from .agents.jev_agent import JevAgent, deck_line
from .models import DecisionTrace, Match, Seat

logger = logging.getLogger(__name__)

DEFAULT_WORKERS = 4
LEASE_WAIT = 30
# How long a decision for a game not yet linked to a match waits for the link:
# the bridge starts a game before the app stores its id.
UNKNOWN_GAME_GRACE = 5.0
# Past either budget, model-backed seats delegate every remaining decision.
DEFAULT_MAX_AGENT_CALLS = 1000
DEFAULT_MAX_MATCH_SECONDS = 1800
# Pause after the bridge could not be reached, before polling again.
ERROR_BACKOFF = 2.0
# A priority decision's option 0 passes priority.
PASS = 0


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def worker_count() -> int:
    return max(_env_int("FORGE_WORKERS", DEFAULT_WORKERS), 0)


def max_agent_calls() -> int:
    return _env_int("MAX_AGENT_CALLS", DEFAULT_MAX_AGENT_CALLS)


def max_match_seconds() -> int:
    return _env_int("MAX_MATCH_SECONDS", DEFAULT_MAX_MATCH_SECONDS)


def deck_list(seat: Seat) -> str:
    """The seat's deck as Argentum tells Jev: the main deck and commander by name and
    quantity, no order, and the archetype (the metagame deck's, or the deck's name)."""
    from .runner import seat_cards  # the runner imports this module

    try:
        cards = seat_cards(seat)
    except forge.ForgeError:
        return ""
    counts: dict[str, int] = {}
    for item in cards:
        if item.get("board", "main") == "main" or item.get("is_commander"):
            name = item["data"]["name"]
            counts[name] = counts.get(name, 0) + int(item.get("quantity") or 0)
    archetype = seat.metagame_deck.archetype if seat.metagame_deck_id and seat.metagame_deck else seat.deck_name
    return deck_line(counts, archetype or "")


def build_agent(seat: Seat):
    if seat.agent == Seat.Agent.JEV:
        return JevAgent(seat.model or typesafe.MODEL, deck=deck_list(seat))
    # forge_ai seats never reach a worker; anything else is Forge's AI too.
    return None


@dataclass
class SeatAgent:
    agent: object
    calls: int = 0
    # The seat's last priority decision, as a fingerprint, and the play picked in it.
    last_priority: str = ""
    last_play: str = ""
    # The plays of that very decision Forge did not carry out.
    failed: set = field(default_factory=set)
    # The trace of the seat's last decision, marked no_effect when that play fails.
    last_trace: int | None = None


@dataclass
class AgentCache:
    """One agent per (match, seat), kept for the whole game.

    Agents hold their session (the picks Jev made so far in a step): the same
    agent answering the game's decisions in order is what keeps it coherent.
    A game's decisions come one at a time, so one entry is used by one worker
    at a time — unless an agent outlives the bridge's DECISION_TIMEOUT, when
    the next decision may arrive meanwhile and that game stops replaying exactly.
    """

    entries: dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self, match: Match, seat: Seat) -> SeatAgent:
        key = (match.pk, seat.position)
        with self.lock:
            if key not in self.entries:
                self.entries[key] = SeatAgent(build_agent(seat))
            return self.entries[key]

    def forget(self, match_id) -> None:
        with self.lock:
            for key in [k for k in self.entries if k[0] == match_id]:
                del self.entries[key]


agents = AgentCache()


def _find_match(game_id: str, grace: float) -> Match | None:
    deadline = time.monotonic() + grace
    while True:
        match = Match.objects.filter(forge_game_id=game_id).first()
        if match is not None or time.monotonic() >= deadline:
            return match
        time.sleep(0.2)


def _over_budget(match: Match, entry: SeatAgent) -> str:
    if entry.calls >= max_agent_calls():
        return f"agent call budget spent ({max_agent_calls()} calls)"
    started = match.started_at or match.created_at
    if (timezone.now() - started).total_seconds() >= max_match_seconds():
        return f"match wall clock spent ({max_match_seconds()} s)"
    return ""


def _fingerprint(decision: dict) -> str:
    return json.dumps(
        [decision.get("prompt"), decision["view"], [option["label"] for option in decision["options"]]],
        sort_keys=True,
        default=str,
    )


def _drop_failed_plays(entry: SeatAgent, decision: dict) -> tuple[str, list[dict]]:
    """``(the play that just failed, the options left)`` of a priority decision.

    When a seat's priority decision comes back identical — same prompt, same
    view, same options — right after it picked a play, Forge did not carry
    that play out (the cast aborted before reaching the stack): it is left
    out, along with the plays that failed before it in the same decision.
    """
    fingerprint = _fingerprint(decision)
    failed_play = ""
    if fingerprint != entry.last_priority:
        entry.failed = set()
    elif entry.last_play:
        failed_play = entry.last_play
        entry.failed.add(failed_play)
    entry.last_priority = fingerprint
    return failed_play, [option for option in decision["options"] if option["label"] not in entry.failed]


def _ask(match: Match, entry: SeatAgent, decision: dict, options: list[dict]) -> Decision:
    if getattr(entry.agent, "metered", False):
        spent = _over_budget(match, entry)
        if spent:
            return delegate("budget", spent)
        entry.calls += 1
    return entry.agent.choose(
        decision["view"], options, kind=decision.get("kind", ""), prompt=decision.get("prompt", "")
    )


def _forge_ai_choice(decision: dict) -> int | None:
    """The option Forge's AI would pick, when the bridge says (#75): an offered index, else None."""
    value = decision.get("forge_ai_choice")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if any(option["index"] == value for option in decision["options"]) else None


def _record(match: Match, seat: Seat, decision: dict, answer: Decision) -> Decision:
    """Stores the trace of a decision the agent was asked (#72); the answer then carries its id.

    A trace that cannot be stored is logged and skipped: it never costs the decision.
    """
    if answer.trace is None:
        return answer
    view = decision.get("view") or {}
    try:
        trace = DecisionTrace.objects.create(
            match=match,
            seat_position=seat.position,
            decision_id=str(decision.get("decision_id") or "")[:64],
            turn=view.get("turn") or 0,
            step=(view.get("phase") or "")[:32],
            kind=(decision.get("kind") or "")[:32],
            prompt=decision.get("prompt") or "",
            instructions=answer.trace.get("instructions") or "",
            state=answer.trace.get("state") or {},
            options=answer.trace.get("options") or [],
            probabilities=answer.trace.get("probabilities") or {},
            latency_ms=answer.trace.get("latency_ms"),
            confidence=answer.confidence,
            choice=answer.choice,
            fallback_kind=answer.fallback_kind,
            fallback_reason=answer.fallback_reason,
            failed_play=answer.failed_play,
            model=answer.model[:128],
            forge_ai_choice=_forge_ai_choice(decision),
        )
    except DatabaseError:
        logger.exception("Could not store the trace of decision %s", decision.get("decision_id"))
        return answer
    return replace(answer, trace_id=trace.pk)


def decide(decision: dict, *, cache: AgentCache = agents, grace: float = UNKNOWN_GAME_GRACE) -> Decision:
    """What the seat's agent answers to one bridge decision."""
    match = _find_match(decision["game_id"], grace)
    if match is None:
        return delegate("error", "no match for this game")
    if not match.is_busy:
        # A failed or stopped match whose game still runs: nobody plays it.
        return delegate("error", f"the match is {match.status}")
    seat = match.seats.filter(position=decision["seat"]).first()
    if seat is None:
        return delegate("error", f"no seat {decision['seat']} in this match")
    entry = cache.get(match, seat)
    if entry.agent is None:
        return delegate("error", f"a {seat.get_agent_display()} seat has no agent")
    if decision.get("kind") != "priority":
        return _record(match, seat, decision, _ask(match, entry, decision, decision["options"]))
    failed_play, options = _drop_failed_plays(entry, decision)
    if failed_play and entry.last_trace is not None:
        DecisionTrace.objects.filter(pk=entry.last_trace).update(outcome=DecisionTrace.Outcome.NO_EFFECT)
    if failed_play and len(options) > 1 and hasattr(entry.agent, "note"):
        # As Argentum tells Jev when its engine rejects a move.
        entry.agent.note(f"Engine rejected {failed_play}: Forge did not carry it out. Correct the move; do not repeat it.")
    # Every play failed: passing is all that is left.
    answer = Decision(options[0]["index"]) if len(options) == 1 else _ask(match, entry, decision, options)
    entry.last_play = next(
        (o["label"] for o in options if o["index"] == answer.choice and answer.choice != PASS), ""
    )
    if failed_play:
        answer = replace(answer, failed_play=failed_play)
    answer = _record(match, seat, decision, answer)
    entry.last_trace = answer.trace_id
    return answer


def serve(client, decision: dict, *, cache: AgentCache = agents, grace: float = UNKNOWN_GAME_GRACE) -> None:
    """Answers one leased decision. Never raises for an agent's failure: that is delegated."""
    try:
        answer = decide(decision, cache=cache, grace=grace)
    except Exception as exc:  # noqa: BLE001 - an agent bug must not cost the match.
        logger.exception("Agent failed on decision %s", decision.get("decision_id"))
        answer = delegate("error", f"agent error: {exc}")
    traced = answer.trace_id
    if answer.choice is not None and not 0 <= answer.choice < len(decision["options"]):
        answer = delegate("error", f"agent picked option {answer.choice}, which was not offered")
    applied = client.answer(decision["decision_id"], answer.choice, delegate=answer.delegate, meta=answer.meta())
    if not applied:
        logger.info("Decision %s was no longer pending (timed out or game ended)", decision["decision_id"])
    if traced is not None and (not applied or answer.trace_id != traced):
        _not_applied(traced)


def _not_applied(trace_id: int) -> None:
    """The agent's pick never reached the game (the bridge timed out, the game ended, or
    the pick was out of range): its trace is a fallback, neither done nor checked."""
    try:
        DecisionTrace.objects.filter(pk=trace_id).update(
            outcome=DecisionTrace.Outcome.DELEGATED, fallback_reason="the answer did not reach the game"
        )
    except DatabaseError:
        logger.exception("Could not update trace %s", trace_id)


class Pool:
    def __init__(self, size: int, client_factory=None):
        self.size = size
        self.client_factory = client_factory or forge.client
        self.threads: list[threading.Thread] = []
        self.stopping = threading.Event()

    def start(self) -> None:
        for index in range(self.size):
            thread = threading.Thread(target=self._loop, name=f"forge-worker-{index}", daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop(self, timeout: float | None = None) -> None:
        """Stops the workers; with a ``timeout``, waits that long for each to finish its poll."""
        self.stopping.set()
        if timeout is not None:
            for thread in self.threads:
                thread.join(timeout)

    def alive(self) -> bool:
        return any(thread.is_alive() for thread in self.threads)

    def _loop(self) -> None:
        client = self.client_factory()
        while not self.stopping.is_set():
            try:
                decision = client.lease(LEASE_WAIT)
                if decision is None:
                    continue
                close_old_connections()
                try:
                    serve(client, decision)
                finally:
                    close_old_connections()
            except forge.ForgeError as exc:
                logger.warning("Forge worker: %s", exc)
                self.stopping.wait(ERROR_BACKOFF)
            except Exception:  # noqa: BLE001 - keep serving whatever happens.
                logger.exception("Forge worker failed; carrying on")
                self.stopping.wait(ERROR_BACKOFF)


_pool: Pool | None = None
_pool_lock = threading.Lock()


def ensure_started() -> bool:
    """Starts this process's pool once; False when ``FORGE_WORKERS`` is 0."""
    global _pool
    with _pool_lock:
        if _pool is not None and _pool.alive():
            return True
        size = worker_count()
        if size == 0:
            return False
        _pool = Pool(size)
        _pool.start()
        logger.info("Started %s Forge workers", size)
        return True


def stop(timeout: float | None = None) -> None:
    """Stops this process's pool, if one runs (the worker shutting down)."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.stop(timeout)
            _pool = None


def running() -> bool:
    """Whether a pool answers decisions in this process."""
    return _pool is not None and _pool.alive()
