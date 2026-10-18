"""Tests for the match summary, on a game the real bridge played (tests/fixtures/forge_game.json)."""

import json
from pathlib import Path
from types import SimpleNamespace

from playtest.summary import current_lives, fallback_reason, summarize_match

GAME = json.loads((Path(__file__).parent / "fixtures" / "forge_game.json").read_text())


def _seats(*agents):
    return [
        SimpleNamespace(position=position, deck_name=name, agent=agent, coverage={"unsupported_cards": []})
        for position, (name, agent) in enumerate(zip(("Red", "Green"), agents))
    ]


def test_the_summary_reads_forges_log_by_the_seats_names():
    summary = summarize_match(GAME["events"], _seats("jev", "jev"), "modern", GAME["game"])
    red, green = summary["seats"]
    assert summary["winner_seat"] == 1 and summary["end_reason"] == "AllOpponentsLost" and summary["turns"] == 14
    assert (red["result"], green["result"]) == ("lost", "won")
    assert (red["final_life"], green["final_life"]) == (-6, 8)
    assert red["turns_taken"] == green["turns_taken"] == 7
    # "Red triggered …" lines are abilities, not spells.
    assert (red["spells_cast"], green["spells_cast"]) == (5, 10)
    assert red["lands_played"] + green["lands_played"] == 5
    assert len(red["life_by_turn"]) == 14
    assert red["life_by_turn"][0] == 20 and green["life_by_turn"][0] == 17


def test_decisions_are_split_between_the_agent_and_forges_ai():
    summary = summarize_match(GAME["events"], _seats("jev", "jev"), "modern", GAME["game"])
    red, green = summary["seats"]
    assert red["decisions"] == {"agent": 20, "forge_ai": 2, "agent_share": 91}
    assert green["decisions"] == {"agent": 42, "forge_ai": 3, "agent_share": 93}
    assert summary["automatic"] == {"assignCombatDamage": 5}


def test_confidence_and_fallbacks_by_reason():
    events = [
        {"seq": 0, "turn": 1, "seat": 0, "kind": "decision", "text": "priority: Pass priority", "payload": {"confidence": 0.9}},
        {"seq": 1, "turn": 1, "seat": 0, "kind": "decision", "text": "priority: Pass priority", "payload": {"confidence": 0.5}},
        {"seq": 2, "turn": 1, "seat": 0, "kind": "fallback", "text": "", "payload": {"reason": "delegate", "fallback_kind": "low_confidence", "confidence": 0.1}},
        {"seq": 3, "turn": 2, "seat": 0, "kind": "fallback", "text": "", "payload": {"reason": "timeout"}},
        {"seq": 4, "turn": 2, "seat": 0, "kind": "fallback", "text": "", "payload": {"reason": "delegate", "fallback_kind": "error"}},
    ]
    seat = summarize_match(events, _seats("jev", "forge_ai"), "duel", {"turn": 2})["seats"][0]
    assert seat["confidence"] == {"rated": 3, "median": 0.5, "low": 1}
    assert seat["fallbacks"] == {"error": 1, "low_confidence": 1, "timeout": 1}
    assert seat["decisions"]["agent_share"] == 40
    assert seat["result"] == "draw"


def test_a_fallbacks_reason_is_the_agents_when_it_delegated():
    assert fallback_reason({"reason": "delegate", "fallback_kind": "budget"}) == "budget"
    assert fallback_reason({"reason": "delegate"}) == "delegate"
    assert fallback_reason({"reason": "too_many_options"}) == "too_many_options"


def test_the_summary_is_identical_for_identical_events():
    seats = _seats("jev", "jev")
    first = summarize_match(GAME["events"], seats, "modern", GAME["game"])
    second = summarize_match(json.loads(json.dumps(GAME["events"])), seats, "modern", dict(GAME["game"]))
    assert first == second
    assert json.loads(json.dumps(first)) == first


def test_a_fallback_and_the_forge_ai_decision_carrying_it_out_count_once():
    events = [
        {"seq": 0, "turn": 1, "seat": 0, "kind": "fallback", "text": "", "payload": {"reason": "delegate", "fallback_kind": "low_confidence"}},
        {"seq": 1, "turn": 1, "seat": None, "kind": "log", "text": "Turn 1 (Red)", "payload": {"type": "TURN"}},
        {"seq": 2, "turn": 1, "seat": 1, "kind": "decision", "text": "", "payload": {}},
        {"seq": 3, "turn": 1, "seat": 0, "kind": "forge_ai", "text": "", "payload": {"method": "chooseSpellAbilityToPlay"}},
        {"seq": 4, "turn": 1, "seat": 0, "kind": "forge_ai", "text": "", "payload": {"method": "assignCombatDamage"}},
    ]
    summary = summarize_match(events, _seats("jev", "forge_ai"), "modern", {"turn": 1})
    assert summary["seats"][0]["decisions"] == {"agent": 0, "forge_ai": 2, "agent_share": 0}
    assert summary["automatic"] == {"assignCombatDamage": 1}


def test_current_lives_follow_the_life_lines_seen_so_far():
    seats = _seats("jev", "jev")
    assert current_lives([], seats) == {}
    assert current_lives(GAME["events"], seats) == {0: -6, 1: 8}
    first = next(i for i, e in enumerate(GAME["events"]) if (e.get("payload") or {}).get("type") == "LIFE")
    assert len(current_lives(GAME["events"][: first + 1], seats)) == 1


def test_plays_forge_did_not_carry_out_are_counted_and_still_decisions():
    stifle = "Cast Stifle #180 for {U}: Counter target activated or triggered ability."
    events = [
        {"seq": 0, "turn": 3, "seat": 0, "kind": "decision", "text": f"priority: {stifle}", "payload": {"kind": "priority"}},
        {"seq": 1, "turn": 3, "seat": 0, "kind": "decision", "text": "priority: Pass priority", "payload": {"kind": "priority", "failed_play": stifle}},
        {"seq": 2, "turn": 3, "seat": 1, "kind": "fallback", "text": "", "payload": {"reason": "delegate", "fallback_kind": "budget", "failed_play": stifle}},
    ]
    red, green = summarize_match(events, _seats("jev", "jev"), "modern", {"turn": 3})["seats"]
    assert red["failed_plays"] == 1 and red["decisions"]["agent"] == 2
    assert green["failed_plays"] == 1 and green["decisions"]["forge_ai"] == 1


def test_a_bridge_reporting_failed_plays_is_counted_once():
    stifle = "Cast Stifle #180 for {U}: Counter target activated or triggered ability."
    events = [
        {"seq": 0, "turn": 3, "seat": 0, "kind": "decision", "text": f"priority: {stifle}", "payload": {"kind": "priority"}},
        {"seq": 1, "turn": 3, "seat": 0, "kind": "play_failed", "text": "", "payload": {"ability": stifle, "reason": "targets"}},
        {"seq": 2, "turn": 3, "seat": 0, "kind": "decision", "text": "priority: Pass priority", "payload": {"kind": "priority", "failed_play": stifle}},
    ]
    red, _ = summarize_match(events, _seats("jev", "jev"), "modern", {"turn": 3})["seats"]
    assert red["failed_plays"] == 1 and red["decisions"]["agent"] == 2


def test_low_confidence_counts_the_decisions_under_the_threshold():
    from playtest.summary import LOW_CONFIDENCE, summarize_match

    from .factories import make_match  # noqa: F401 - documents where seats come from in the other tests

    class Seat:
        def __init__(self, position):
            self.position, self.deck_name, self.agent, self.coverage = position, f"D{position}", "jev", {}

    events = [
        {"seq": 0, "turn": 1, "seat": 0, "kind": "decision", "text": "", "payload": {"confidence": 0.9}},
        {"seq": 1, "turn": 1, "seat": 0, "kind": "decision", "text": "", "payload": {"confidence": LOW_CONFIDENCE - 0.05}},
        {"seq": 2, "turn": 1, "seat": 0, "kind": "fallback", "text": "", "payload": {"reason": "delegate", "fallback_kind": "error"}},
    ]
    summary = summarize_match(events, [Seat(0), Seat(1)], "modern", {"status": "finished", "winner": 0, "turn": 1})
    assert summary["seats"][0]["confidence"] == {"rated": 2, "median": 0.525, "low": 1}
