"""Tests for the Jev agent on Forge's decisions (no network): it plays Jev the way Argentum Engine does."""

import json

import pytest

from playtest.agents import typesafe
from playtest.agents.jev_agent import (
    ARGENTUM_LINE,
    MAX_REQUEST_BYTES,
    QUESTION,
    JevAgent,
    deck_line,
    filtered_state,
    jev_state,
)

from .fakeforge import sample_decision


class StubJev:
    """Answers like Jev: picks the option ``prefer`` says, with a given confidence."""

    def __init__(self, prefer=None, confidence=0.9, fail=None):
        self.prefer = prefer or (lambda criteria: sorted(criteria)[-1])
        self.confidence = confidence
        self.fail = fail
        self.requests = []

    def __call__(self, state, questions, model=None):
        self.requests.append((state, questions, model))
        if self.fail:
            raise typesafe.TypeSafeError(self.fail)
        criteria = questions[QUESTION]["criteria"]
        pick = self.prefer(criteria)
        rest = (1 - self.confidence) / max(len(criteria) - 1, 1)
        probabilities = {key: (self.confidence if key == pick else rest) for key in criteria}
        return {
            "model": "typesafe/jev-1.13",
            "answers": {QUESTION: {"type": "choice", "choice": pick, "confidence": self.confidence, "probabilities": probabilities}},
        }


def _choose(agent, decision=None, **changes):
    decision = decision or sample_decision()
    decision.update(changes)
    return agent.choose(decision["view"], decision["options"], kind=decision["kind"], prompt=decision["prompt"])


def _lines(request) -> list[str]:
    """The state Jev was sent, line by line: deck, view, then the session's history."""
    return request[0].split("\n")


DECK = deck_line({"Bear": 4})


def _history(request) -> list[str]:
    """The session's lines of a request sent with ``DECK``."""
    lines = _lines(request)
    assert lines[0] == DECK
    return lines[2:]


def test_options_keep_forges_order_under_opaque_keys_described_by_their_label():
    decision = sample_decision()
    jev = StubJev(prefer=lambda criteria: next(k for k, v in criteria.items() if v.startswith("Attack with Bear")))
    picked = _choose(JevAgent("~typesafe/jev-latest", ask=jev), decision)
    assert picked.choice == 1 and not picked.delegate
    [(_, questions, model)] = jev.requests
    assert model == "~typesafe/jev-latest" and list(questions) == [QUESTION]
    criteria = questions[QUESTION]["criteria"]
    assert list(criteria) == ["c0", "c1", "c2"]
    # The label alone, as Argentum describes its legal actions: Forge's facts stay in the trace.
    assert criteria["c1"] == "Attack with Bear #3 (3/3) -> Green"


def test_the_instructions_are_forges_prompt_and_argentums_line():
    jev = StubJev()
    _choose(JevAgent(ask=jev))
    assert jev.requests[0][1][QUESTION]["instructions"] == f"Declare attackers. {ARGENTUM_LINE}"
    _choose(JevAgent(ask=jev), prompt="")
    assert jev.requests[1][1][QUESTION]["instructions"].startswith("Choose your next play. ")


def test_the_state_is_the_deck_line_and_the_whole_view():
    jev = StubJev()
    deck = deck_line({"Shock": 4, "Mountain": 20}, "Burn")
    assert deck == "Deck (order unknown): {Shock=4, Mountain=20}. Archetype: Burn"
    _choose(JevAgent(ask=jev, deck=deck))
    lines = _lines(jev.requests[0])
    assert lines[0] == deck and len(lines) == 2
    view = json.loads(lines[1])
    # The whole view: ids, graveyard cards, the opponent's battlefield.
    assert view["turn"] == 3 and view["you"]["graveyard"][0] == {
        "id": 70, "name": "Lightning Bolt", "mana_cost": "{R}", "types": "Instant", "text": "Lightning Bolt deals 3 damage to any target."}
    assert view["battlefield"]["1"][0]["name"] == "Wall"
    assert lines[1] == json.dumps(view, separators=(",", ":"), ensure_ascii=False)


def test_without_a_deck_the_state_starts_with_the_view():
    jev = StubJev()
    _choose(JevAgent(ask=jev))
    assert _lines(jev.requests[0])[0].startswith("{")


def test_later_picks_of_a_session_see_the_earlier_ones():
    """Declaring the second attacker, Jev reads what it did with the first."""
    jev = StubJev(prefer=lambda c: "c1")
    agent = JevAgent(ask=jev, deck=DECK)
    _choose(agent)
    _choose(agent)
    assert _history(jev.requests[1]) == ["Declare attackers: Attack with Bear #3 (3/3) -> Green"]
    _choose(agent)
    assert len(_history(jev.requests[2])) == 2


def test_a_priority_decision_or_a_new_step_starts_a_session():
    jev = StubJev(prefer=lambda c: "c1")
    agent = JevAgent(ask=jev, deck=DECK)
    _choose(agent)
    _choose(agent, kind="priority", prompt="Choose your next play")
    assert _history(jev.requests[1]) == []
    # The target of the spell just cast continues that session…
    _choose(agent, kind="target", prompt="Choose a target")
    assert _history(jev.requests[2]) == ["Choose your next play: Attack with Bear #3 (3/3) -> Green"]
    # …a decision in another step does not.
    later = sample_decision(kind="block")
    later["view"]["phase"] = "COMBAT_DECLARE_BLOCKERS"
    _choose(agent, later)
    assert _history(jev.requests[3]) == []


def test_a_rejected_play_goes_on_in_the_same_session_with_the_workers_note():
    """As Argentum re-asks in the same session: Jev reads its pick and why it was rejected."""
    jev = StubJev(prefer=lambda c: "c1")
    agent = JevAgent(ask=jev, deck=DECK)
    _choose(agent, kind="priority", prompt="Choose your next play")
    agent.note("Engine rejected Cast Shock: Forge did not carry it out. Correct the move; do not repeat it.")
    _choose(agent, kind="priority", prompt="Choose your next play")
    assert _history(jev.requests[1]) == [
        "Choose your next play: Attack with Bear #3 (3/3) -> Green",
        "Engine rejected Cast Shock: Forge did not carry it out. Correct the move; do not repeat it.",
    ]
    _choose(agent, kind="priority", prompt="Choose your next play")
    assert _history(jev.requests[2]) == []


def test_the_cards_to_bottom_share_the_mulligan_session():
    jev = StubJev(prefer=lambda c: "c1")
    agent = JevAgent(ask=jev, deck=DECK)
    hand = sample_decision(kind="mulligan", options=[("Keep this hand", []), ("Mulligan", [])])
    hand["view"]["phase"], hand["prompt"] = "", "Keep or mulligan?"
    _choose(agent, hand)
    bottom = sample_decision(kind="bottom", options=[("Bottom Shock #6", []), ("Bottom Mountain #7", [])])
    bottom["view"]["phase"], bottom["prompt"] = "", "Put a card on the bottom"
    _choose(agent, bottom)
    assert _history(jev.requests[1]) == ["Keep or mulligan?: Mulligan"]


def test_a_single_option_is_taken_without_asking():
    jev = StubJev()
    decision = sample_decision(kind="target", options=[("Target Wall #5", [])])
    picked = _choose(JevAgent(ask=jev), decision)
    assert picked.choice == 0 and not picked.delegate and picked.trace is None and not jev.requests


def test_more_than_255_options_are_picked_through_a_group_first():
    options = [(f"Name a card: Card {i}", []) for i in range(300)]
    decision = sample_decision(kind="mode", options=options)
    jev = StubJev(prefer=lambda c: "c1")
    picked = _choose(JevAgent(ask=jev, deck=DECK), decision)
    first, second = jev.requests
    assert len(first[1][QUESTION]["criteria"]) == 150 and first[1][QUESTION]["instructions"].startswith(
        "Declare attackers: choose a group containing your preferred option.")
    assert first[1][QUESTION]["criteria"]["c0"] == "Name a card: Card 0; Name a card: Card 1"
    assert list(second[1][QUESTION]["criteria"].values()) == ["Name a card: Card 2", "Name a card: Card 3"]
    assert picked.choice == 3
    assert _history(second) == ["Declare attackers: choose a group containing your preferred option: Name a card: Card 2; Name a card: Card 3"]


def test_a_request_over_argentums_budget_delegates_to_forges_ai():
    decision = sample_decision()
    decision["view"]["you"]["graveyard"] = [{"id": i, "name": f"Card {i}", "text": "x" * 200} for i in range(200)]
    jev = StubJev()
    picked = _choose(JevAgent(ask=jev), decision)
    assert not jev.requests
    assert picked.delegate and picked.fallback_kind == "error" and str(MAX_REQUEST_BYTES) in picked.fallback_reason
    assert picked.trace["options"] and picked.trace["probabilities"] == {}


def test_the_decision_carries_confidence_probabilities_and_model():
    decision = _choose(JevAgent(ask=StubJev(confidence=0.82)))
    assert decision.confidence == 0.82 and decision.model == "typesafe/jev-1.13"
    meta = decision.meta()
    assert meta["confidence"] == 0.82 and meta["probabilities"][0]["p"] == 0.82
    assert meta["probabilities"][0]["label"] in [o["label"] for o in sample_decision()["options"]]
    assert not decision.delegate


def test_an_unsure_pick_is_still_jevs_pick():
    """No confidence gate: Argentum takes Jev's answer as it is."""
    decision = _choose(JevAgent(ask=StubJev(confidence=0.05)))
    assert not decision.delegate and decision.confidence == 0.05


@pytest.mark.parametrize("jev", [StubJev(fail="HTTP 503"), StubJev(prefer=lambda criteria: "c99")])
def test_a_failure_or_an_answer_outside_the_options_delegates(jev):
    decision = _choose(JevAgent(ask=jev))
    assert decision.delegate and decision.fallback_kind == "error"
    assert decision.trace["options"] and decision.trace["probabilities"] == {}


def test_a_missing_key_delegates(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    decision = _choose(JevAgent())
    assert decision.delegate and decision.fallback_kind == "error"
    assert "OPENROUTER_API_KEY" in decision.fallback_reason


def test_the_trace_keeps_what_jev_saw():
    jev = StubJev(prefer=lambda c: "c1")
    agent = JevAgent(ask=jev, deck=DECK)
    _choose(agent)
    answer = _choose(agent)
    state, questions, _ = jev.requests[1]
    trace = answer.trace
    assert trace["state"] == {
        "deck": "Deck (order unknown): {Bear=4}. Archetype: ",
        "view": json.loads(_lines(jev.requests[1])[1]),
        "history": ["Declare attackers: Attack with Bear #3 (3/3) -> Green"],
    }
    assert trace["instructions"] == questions[QUESTION]["instructions"]
    assert [o["index"] for o in trace["options"]] == [0, 1, 2]
    assert trace["options"][1]["facts"] == ["deals 3 damage if unblocked"]
    assert set(trace["probabilities"]) == {"0", "1", "2"}
    assert abs(sum(trace["probabilities"].values()) - 1) < 1e-3
    assert isinstance(trace["latency_ms"], int)
    assert "trace" not in answer.meta()


def test_the_filtered_state_reads_a_stored_trace_state():
    """What the checks, the export and the inspector read: the view filtered, whatever the stored shape."""
    view = sample_decision()["view"]
    stored = {"deck": "Deck (order unknown): {}. Archetype: ", "view": view, "history": ["a: b"]}
    state = filtered_state(stored)
    assert state["you"]["life"] == 20 and state["active_player"] == "you" and state["you"]["graveyard"] == 1
    assert state["history"] == ["a: b"] and "view" not in state
    old = json.loads(jev_state(view))
    assert filtered_state(old) == old
    assert filtered_state({}) == {}


def test_the_filtered_view_counts_zones_unless_the_decision_is_about_them():
    view = sample_decision()["view"]
    state = json.loads(jev_state(view, kind="attack", prompt="Declare attackers"))
    assert state["you"]["hand"][0] == {"name": "Shock", "mana_cost": "{R}", "types": "Instant", "text": "Shock deals 2 damage to any target."}
    wall, rancor = state["opponents"][0]["battlefield"]
    assert wall["summoning_sick"] and wall["counters"] == {"P1P1": 1} and "tapped" not in wall
    assert rancor["attached_to"] == "Wall"
    assert state["you"]["graveyard"] == 1 and '"id"' not in jev_state(view)
    about = json.loads(jev_state(view, kind="choose_cards", prompt="Return a card from your graveyard"))
    assert about["you"]["graveyard"][0]["name"] == "Lightning Bolt"
