"""Tests for the Jev agent on Forge's decisions (no network)."""

import json

import pytest

from playtest.agents import typesafe
from playtest.agents.jev_agent import JevAgent, describe_option, instructions, jev_state

from .fakeforge import sample_decision


class StubJev:
    """Answers like Jev: picks the option ``prefer`` says, with a given confidence, and
    rates a ``score`` question with ``scores[id]`` (a dict or a callable of the id; 3.0 by default)."""

    def __init__(self, prefer=None, confidence=0.9, fail=None, scores=None):
        self.prefer = prefer or (lambda criteria: sorted(criteria)[-1])
        self.confidence = confidence
        self.fail = fail
        self.scores = scores
        self.requests = []

    def _score(self, question_id, question):
        if callable(self.scores):
            value = self.scores(question_id)
        else:
            value = (self.scores or {}).get(question_id, 3.0)
        if value is None:
            return None
        levels = len(question["criteria"])
        probabilities = {str(n): (1.0 if n == round(value) else 0.0) for n in range(levels)}
        return {"type": "score", "score": value, "probabilities": probabilities, "confidence": 0.8}

    def __call__(self, state, questions, model=None):
        self.requests.append((state, questions, model))
        if self.fail:
            raise typesafe.TypeSafeError(self.fail)
        answers = {}
        for question_id, question in questions.items():
            if question["type"] == "score":
                answer = self._score(question_id, question)
                if answer is not None:
                    answers[question_id] = answer
                continue
            criteria = question["criteria"]
            pick = self.prefer(criteria)
            rest = (1 - self.confidence) / max(len(criteria) - 1, 1)
            probabilities = {key: (self.confidence if key == pick else rest) for key in criteria}
            answers[question_id] = {"type": "choice", "choice": pick, "confidence": self.confidence, "probabilities": probabilities}
        return {"model": "typesafe/jev-1.13", "answers": answers}


def _choose(agent, decision=None):
    decision = decision or sample_decision()
    return agent.choose(decision["view"], decision["options"], kind=decision["kind"], prompt=decision["prompt"],
                        simulated=decision.get("simulated", False))


def test_the_options_are_exactly_the_bridges_shuffled_and_mapped_back():
    decision = sample_decision()
    jev = StubJev(prefer=lambda criteria: next(k for k, v in criteria.items() if v.startswith("Attack with Bear")))
    picked = _choose(JevAgent("~typesafe/jev-latest", seed=3, ask=jev), decision)
    assert picked.choice == 1
    [(_, questions, model)] = jev.requests
    criteria = questions["action"]["criteria"]
    assert model == "~typesafe/jev-latest"
    assert sorted(criteria) == ["o0", "o1", "o2"]
    # The order Jev sees is a seeded shuffle, not Forge's order ("Done" first)…
    orders = set()
    for seed in range(8):
        stub = StubJev()
        _choose(JevAgent(seed=seed, ask=stub))
        orders.add(tuple(stub.requests[0][1]["action"]["criteria"]))
    assert len(orders) > 1
    # …and the same seed shuffles the same way.
    again = [StubJev(), StubJev()]
    for stub in again:
        _choose(JevAgent(seed=5, ask=stub))
    assert list(again[0].requests[0][1]["action"]["criteria"]) == list(again[1].requests[0][1]["action"]["criteria"])


def test_an_option_is_described_by_its_label_and_forges_facts():
    option = sample_decision()["options"][1]
    assert describe_option(option) == "Attack with Bear #3 (3/3) -> Green — deals 3 damage if unblocked"
    assert describe_option({"index": 0, "label": "Pass priority", "facts": []}) == "Pass priority"


def test_the_instructions_name_the_format_the_turn_the_kind_and_forges_prompt():
    jev = StubJev()
    _choose(JevAgent(seed=1, ask=jev))
    text = jev.requests[0][1]["action"]["instructions"]
    assert "(Modern)" in text and "turn 3, your turn" in text
    assert "Declare attackers one at a time" in text
    assert 'Forge asks: "Declare attackers"' in text


def _priority_view(phase, active=0, stack=()):
    view = sample_decision()["view"]
    view["phase"], view["active_seat"], view["stack"] = phase, active, list(stack)
    return view


def test_a_priority_decision_says_what_passing_means_in_this_step():
    text = lambda phase, active=0, stack=(): instructions(_priority_view(phase, active, stack), "priority", "x")  # noqa: E731
    assert "passing moves on to your first main phase" in text("UPKEEP")
    assert "passing moves on to your first main phase" in text("DRAW")
    assert "play a land if you have not" in text("MAIN1")
    assert "act only to change this combat" in text("COMBAT_DECLARE_ATTACKERS")
    assert "passing with an empty stack ends your turn" in text("MAIN2")
    # The opponent's turn gets no step line: one telling Jev to pass unless answering made it
    # passive with instants (benchmark of 2026-10-17).
    assert "otherwise pass" not in text("MAIN1", active=1) and "You have priority" in text("MAIN1", active=1)
    assert "passing lets the top of the stack resolve" in text("MAIN1", stack=[{"text": "Shock"}])
    # Only priority decisions get a step line.
    assert "ends your turn" not in instructions(_priority_view("MAIN2"), "attack", "Declare attackers")


def test_the_command_zone_prompt_says_what_no_costs():
    prompt = ("Aragorn, King of Gondor: If a commander is in a graveyard or in exile and that card was put into "
              "that zone since the last time state-based actions were checked, its owner may put it into the command zone.")
    text = instructions(_priority_view("MAIN1"), "confirm", prompt)
    assert "Yes moves your commander to the command zone" in text
    assert "command zone" not in instructions(_priority_view("MAIN1"), "confirm", "Use the optional trigger of X")


def test_the_state_is_the_view_filtered_to_the_decision():
    decision = sample_decision()
    state = json.loads(jev_state(decision["view"], kind="attack", prompt="Declare attackers"))
    assert state["you"]["life"] == 20 and state["opponents"][0]["life"] == 20
    assert state["you"]["hand"][0] == {"name": "Shock", "mana_cost": "{R}", "types": "Instant", "text": "Shock deals 2 damage to any target."}
    wall, rancor = state["opponents"][0]["battlefield"]
    assert wall["summoning_sick"] and wall["counters"] == {"P1P1": 1} and "tapped" not in wall
    assert rancor["attached_to"] == "Wall"
    # Graveyards are counts unless the decision is about them; no ids anywhere.
    assert state["you"]["graveyard"] == 1
    assert '"id"' not in jev_state(decision["view"])
    about = json.loads(jev_state(decision["view"], kind="choose_cards", prompt="Return a card from your graveyard"))
    assert about["you"]["graveyard"][0]["name"] == "Lightning Bolt"


def test_the_decision_carries_confidence_probabilities_and_model():
    decision = _choose(JevAgent(seed=1, ask=StubJev(confidence=0.82)))
    assert decision.confidence == 0.82 and decision.model == "typesafe/jev-1.13"
    meta = decision.meta()
    assert meta["confidence"] == 0.82 and meta["probabilities"][0]["p"] == 0.82
    assert meta["probabilities"][0]["label"] in [o["label"] for o in sample_decision()["options"]]
    assert not decision.delegate


def test_a_failure_delegates_to_forges_ai():
    answer = _choose(JevAgent(seed=1, ask=StubJev(fail="HTTP 503")))
    assert answer.delegate and answer.fallback_kind == "error" and "HTTP 503" in answer.fallback_reason


def test_doubt_no_longer_delegates():
    answer = _choose(JevAgent(seed=1, ask=StubJev(confidence=0.05)))
    assert answer.choice is not None and not answer.fallback_kind and answer.confidence == 0.05


def _scored(prefer="o0", scores=None, confidence=0.9, decision=None):
    jev = StubJev(prefer=lambda c: prefer, scores=scores, confidence=confidence)
    return _choose(JevAgent(seed=1, ask=jev), decision or sample_decision(simulated=True)), jev


def test_a_simulated_decision_is_scored_and_the_best_outcome_wins():
    answer, jev = _scored(prefer="o0", scores={"s0": 3.0, "s1": 4.2, "s2": 3.5})
    assert answer.choice == 1
    assert dict(answer.scores)["Attack with Bear #3 (3/3) -> Player Green (20 life)"] == 4.2
    [(state, questions, _)] = jev.requests
    assert set(questions) == {"action", "s0", "s1", "s2"} and questions["s1"]["type"] == "score"
    assert len(questions["s1"]["criteria"]) == 7 and "o1" in questions["s1"]["instructions"]
    assert json.loads(state)["candidates"]["o1"]["outcome"]["opponents"][0]["life"] == [20, 17]
    assert "Pick your whole attack" in questions["action"]["instructions"]
    assert answer.meta()["scores"][1] == {"label": "Attack with Bear #3 (3/3) -> Player Green (20 life)", "score": 4.2}


def test_ties_go_to_the_choice():
    answer, _ = _scored(prefer="o2", scores={"s0": 1.0, "s1": 4.0, "s2": 4.05})
    assert answer.choice == 2
    answer, _ = _scored(prefer="o1", scores={"s0": 1.0, "s1": 4.0, "s2": 4.05})
    assert answer.choice == 1


def test_a_missing_score_leaves_the_option_to_the_choice():
    scores = lambda q: {"s0": 2.0, "s1": 4.0}.get(q)  # noqa: E731 - s2 is never answered
    answer, _ = _scored(prefer="o2", scores=scores, confidence=0.6)
    assert answer.choice == 2
    answer, _ = _scored(prefer="o2", scores=scores, confidence=0.4)
    assert answer.choice == 1


def test_a_simulated_decision_without_outcomes_is_a_plain_choice():
    decision = sample_decision(simulated=True)
    for option in decision["options"]:
        option["outcome"] = None
    answer, jev = _scored(prefer="o2", decision=decision)
    assert answer.choice == 2 and set(jev.requests[0][1]) == {"action"}


def test_a_decision_without_the_simulated_key_is_a_plain_choice():
    answer, jev = _scored(prefer="o1", decision=sample_decision())
    assert answer.choice == 1 and set(jev.requests[0][1]) == {"action"} and answer.scores == ()
    assert "one at a time" in jev.requests[0][1]["action"]["instructions"]


def test_a_missing_key_delegates(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    decision = _choose(JevAgent(seed=1))
    assert decision.delegate and decision.fallback_kind == "error"
    assert "OPENROUTER_API_KEY" in decision.fallback_reason


def test_an_answer_outside_the_options_delegates():
    decision = _choose(JevAgent(seed=1, ask=StubJev(prefer=lambda criteria: "o99")))
    assert decision.delegate and "not an option" in decision.fallback_reason


def test_the_trace_keeps_what_jev_saw_in_the_order_it_saw_it():
    jev = StubJev(confidence=0.9)
    answer = _choose(JevAgent(seed=3, ask=jev))
    state, questions, _ = jev.requests[0]
    trace = answer.trace
    assert trace["state"] == json.loads(state)
    assert trace["instructions"] == questions["action"]["instructions"]
    shown = [f"o{option['index']}" for option in trace["options"]]
    assert shown == list(questions["action"]["criteria"])
    assert trace["options"][1]["facts"] is not None
    assert set(trace["probabilities"]) == {"0", "1", "2"}
    assert abs(sum(trace["probabilities"].values()) - 1) < 1e-3
    assert isinstance(trace["latency_ms"], int)
    assert "trace" not in answer.meta()


def test_a_delegated_decision_keeps_its_trace():
    failed = _choose(JevAgent(seed=3, ask=StubJev(fail="down")))
    assert failed.delegate and failed.trace["options"] and failed.trace["probabilities"] == {}
    assert failed.trace["instructions"] and failed.trace["latency_ms"] is not None
