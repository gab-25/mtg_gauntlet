"""The decision trace: what Jev saw and answered, kept for every decision it was asked (#72)."""

import pytest

from playtest import runner, workers
from playtest.agents import typesafe
from playtest.agents.base import Decision, delegate
from playtest.models import DecisionTrace, Match, Seat

from .factories import make_constructed_deck, make_match, make_playtest
from .test_jev_agent import StubJev


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="u")


def test_meta_carries_the_trace_id_but_never_the_trace():
    answer = Decision(1, confidence=0.9, model="m", trace={"state": {"big": True}}, trace_id=7)
    assert answer.meta() == {"confidence": 0.9, "model": "m", "trace": 7}
    assert "trace" not in Decision(1, trace={"state": {}}).meta()
    assert delegate("error", "x", trace={"a": 1}).trace == {"a": 1}


@pytest.mark.django_db
def test_deleting_a_match_deletes_its_traces(user):
    match = make_match(user)
    DecisionTrace.objects.create(match=match, seat_position=0, kind="priority")
    match.delete()
    assert not DecisionTrace.objects.exists()


@pytest.fixture
def jev(monkeypatch):
    stub = StubJev(confidence=0.9)
    monkeypatch.setattr(typesafe, "system_one", stub)
    return stub


def _played(user, agents=("jev", "jev"), seed=5):
    match = Match.objects.create(owner=user, playtest=make_playtest(user, fmt="modern", seed=seed), format="modern", seed=seed)
    for position, agent in enumerate(agents):
        deck = make_constructed_deck(user, name=f"Deck {position}")
        Seat.objects.create(match=match, position=position, deck=deck, deck_name=deck.name, agent=agent)
    runner.run_match(match.id)
    match.refresh_from_db()
    return match


@pytest.mark.django_db
def test_every_jev_decision_has_a_trace_linked_to_its_event(user, fake_forge, jev):
    match = _played(user)
    assert match.status == Match.Status.FINISHED, match.error
    decisions = list(match.events.filter(kind="decision"))
    traces = list(match.traces.all())
    assert len(traces) == len(decisions) == len(jev.requests)
    for trace, event in zip(traces, decisions):
        assert trace.event_id == event.id and event.payload["trace"] == trace.id
        assert trace.choice == event.payload["choice"] and trace.kind == event.payload["kind"]
        assert trace.seat_position == event.seat_position and trace.state["you"]["life"]
        assert trace.outcome == "done" and trace.model == "typesafe/jev-1.13"
    sent = fake_forge.answers[0]["meta"]
    assert set(sent) == {"confidence", "probabilities", "model", "trace"}


@pytest.mark.django_db
def test_a_delegated_decision_is_traced_as_delegated(user, fake_forge, jev):
    jev.fail = "HTTP 503"
    match = _played(user, agents=("jev", "forge_ai"))
    traces = list(match.traces.all())
    assert traces and all(t.outcome == "delegated" and t.choice is None for t in traces)
    assert all(t.fallback_kind == "error" and t.event.kind == "fallback" for t in traces)


@pytest.mark.django_db
def test_an_unreachable_jev_still_leaves_a_trace(user, fake_forge, jev):
    jev.fail = "down"
    match = _played(user)
    trace = match.traces.first()
    assert trace.fallback_kind == "error" and trace.options and trace.probabilities == {}
    assert trace.outcome == "delegated"


@pytest.mark.django_db
def test_a_play_forge_did_not_carry_out_is_traced_as_no_effect(user, fake_forge, jev):
    fake_forge.failing_cast = True
    match = _played(user)
    no_effect = list(match.traces.filter(outcome="no_effect"))
    assert no_effect and all("Cast Stifle" in t.chosen_label for t in no_effect)
    retried = match.traces.exclude(failed_play="")
    assert retried.count() == len(no_effect)


@pytest.mark.django_db
def test_no_trace_when_jev_is_not_asked(user, fake_forge, jev, monkeypatch):
    monkeypatch.setenv("MAX_AGENT_CALLS", "0")
    match = _played(user)
    assert not jev.requests and not match.traces.exists()


@pytest.mark.django_db
def test_a_trace_that_cannot_be_stored_does_not_cost_the_decision(user, fake_forge, jev, monkeypatch):
    from django.db import DatabaseError

    def broken(**kwargs):
        raise DatabaseError("disk full")

    monkeypatch.setattr(workers.DecisionTrace.objects, "create", broken)
    match = _played(user)
    assert match.status == Match.Status.FINISHED, match.error
    assert match.events.filter(kind="decision").exists() and not match.traces.exists()
    assert all("trace" not in a["meta"] for a in fake_forge.answers)


@pytest.mark.django_db
def test_the_checks_run_when_the_match_ends(user, fake_forge, jev):
    fake_forge.failing_cast = True
    match = _played(user)
    flagged = list(match.traces.exclude(flags=[]))
    assert flagged and all(flag["reason"] for t in flagged for flag in t.flags)
    assert {flag["check"] for t in flagged for flag in t.flags} >= {"no_effect"}
    total = sum(sum(seat["flags"].values()) for seat in match.summary["seats"])
    assert total == sum(len(t.flags) for t in flagged)


@pytest.mark.django_db
def test_a_match_without_traces_has_no_flags_in_its_summary(user, fake_forge, jev, monkeypatch):
    monkeypatch.setenv("MAX_AGENT_CALLS", "0")
    match = _played(user)
    assert all("flags" not in seat for seat in match.summary["seats"])


@pytest.mark.django_db
def test_forge_ais_choice_from_the_lease_is_kept_and_compared(user, fake_forge, jev):
    fake_forge.forge_ai_choice = lambda decision: 0
    match = _played(user)
    traces = list(match.traces.all())
    assert traces and all(t.forge_ai_choice == 0 for t in traces)
    agreement = match.summary["seats"][0]["agreement"]
    rated = [t for t in traces if t.seat_position == 0 and t.choice is not None]
    assert agreement["rated"] == len(rated)
    assert agreement["agree"] == sum(t.choice == 0 for t in rated)


@pytest.mark.django_db
@pytest.mark.parametrize("value", [None, "1", 99, -1, True])
def test_a_missing_or_bad_forge_ai_choice_is_ignored(user, fake_forge, jev, value):
    fake_forge.forge_ai_choice = lambda decision: value
    match = _played(user)
    assert not match.traces.exclude(forge_ai_choice=None).exists()
    assert all("agreement" not in seat for seat in match.summary["seats"])


@pytest.mark.django_db
def test_an_answer_that_never_reached_the_game_is_traced_as_delegated(user, fake_forge, jev, monkeypatch):
    """The bridge's DECISION_TIMEOUT handed the decision to Forge's AI before Jev answered:
    Jev's pick never happened, so it is neither done nor checked."""
    answer = fake_forge.answer

    def too_late(decision_id, choice=None, *, delegate=False, meta=None):
        answer(decision_id, None, delegate=True, meta={})
        return False

    monkeypatch.setattr(fake_forge, "answer", too_late)
    fake_forge.forge_ai_choice = lambda decision: 0
    match = _played(user)
    traces = list(match.traces.all())
    assert traces and all(t.outcome == "delegated" and t.choice is not None for t in traces)
    assert all("did not reach the game" in t.fallback_reason for t in traces)
    assert not any(t.flags for t in traces)
    assert all(not seat.get("flags") and "agreement" not in seat for seat in match.summary["seats"])


@pytest.mark.django_db
def test_a_failing_check_never_costs_the_match_result(user, fake_forge, jev, monkeypatch):
    def broken(traces, fmt):
        raise RuntimeError("check bug")

    monkeypatch.setattr(runner, "check_match", broken)
    match = _played(user)
    assert match.status == Match.Status.FINISHED and match.summary["seats"]
    assert all(t.outcome in ("done", "delegated", "no_effect") for t in match.traces.all())


@pytest.mark.django_db
def test_a_declaration_forge_ai_finished_is_settled_as_delegated(user):
    """The bridge undoes a declaration's earlier picks when a later step delegates:
    Forge's AI declares the whole combat, so none of Jev's picks happened."""
    match = make_match(user)

    def traced(seat, kind, choice, turn=4, step="COMBAT_DECLARE_ATTACKERS"):
        return DecisionTrace.objects.create(match=match, seat_position=seat, turn=turn, step=step, kind=kind, choice=choice)

    undone = [traced(1, "attack", 1), traced(1, "attack", 2)]
    handed = traced(1, "attack", None)
    blocks = traced(0, "block", 1, step="COMBAT_DECLARE_BLOCKERS")
    kept = [traced(1, "attack", 1, turn=6), traced(1, "attack", 0, turn=6)]
    runner.settle_traces(match.pk)
    outcome = lambda t: DecisionTrace.objects.get(pk=t.pk).outcome  # noqa: E731
    assert [outcome(t) for t in undone + [handed]] == ["delegated"] * 3
    assert outcome(blocks) == "done"
    assert [outcome(t) for t in kept] == ["done", "done"]


@pytest.mark.django_db
def test_scores_and_outcomes_are_stored_on_the_trace(user):
    from .fakeforge import sample_decision

    match = make_match(user)
    seat = match.seats.get(position=0)
    decision = sample_decision(simulated=True)
    answer = Decision(1, confidence=0.9, model="m", scores=(("Attack with Bear #3 (3/3) -> Player Green (20 life)", 4.2),),
                      trace={"instructions": "i", "state": {}, "options": decision["options"],
                             "probabilities": {"1": 0.9}, "scores": {"1": 4.2, "0": 3.0}, "latency_ms": 12})
    stored = workers._record(match, seat, decision, answer)
    trace = DecisionTrace.objects.get(pk=stored.trace_id)
    assert trace.scores == {"1": 4.2, "0": 3.0}
    assert trace.options[1]["outcome"]["summary"] == "Green: 20 → 17 life"
    assert DecisionTrace.objects.create(match=match, seat_position=0).scores == {}
