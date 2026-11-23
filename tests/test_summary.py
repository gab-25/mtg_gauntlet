"""Tests for the match summary, on a game the real bridge played (tests/fixtures/forge_game.json)."""

import json
from pathlib import Path
from types import SimpleNamespace

from playtest.summary import current_lives, summarize_match

GAME = json.loads((Path(__file__).parent / "fixtures" / "forge_game.json").read_text())


def _seats():
    return [
        SimpleNamespace(position=position, deck_name=name, coverage={"unsupported_cards": []})
        for position, name in enumerate(("Red", "Green"))
    ]


def test_the_summary_reads_forges_log_by_the_seats_names():
    summary = summarize_match(GAME["events"], _seats(), "modern", GAME["game"])
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


def test_the_summary_is_identical_for_identical_events():
    seats = _seats()
    first = summarize_match(GAME["events"], seats, "modern", GAME["game"])
    second = summarize_match(json.loads(json.dumps(GAME["events"])), seats, "modern", dict(GAME["game"]))
    assert first == second
    assert json.loads(json.dumps(first)) == first


def test_current_lives_follow_the_life_lines_seen_so_far():
    seats = _seats()
    assert current_lives([], seats) == {}
    assert current_lives(GAME["events"], seats) == {0: -6, 1: 8}
    first = next(i for i, e in enumerate(GAME["events"]) if (e.get("payload") or {}).get("type") == "LIFE")
    assert len(current_lives(GAME["events"][: first + 1], seats)) == 1


def test_only_forges_log_counts():
    """The summary reads the log lines; a game_over event changes nothing."""
    events = [
        {"seq": 0, "turn": 1, "seat": None, "kind": "log", "text": "Turn 1 (Red)", "payload": {"type": "TURN"}},
        {"seq": 1, "turn": 1, "seat": None, "kind": "log", "text": "Red played Mountain (3)", "payload": {"type": "LAND"}},
        {"seq": 2, "turn": 1, "seat": None, "kind": "game_over", "text": "Game over", "payload": {"winner": 0}},
    ]
    red, green = summarize_match(events, _seats(), "modern", {"turn": 1, "winner": 0})["seats"]
    assert (red["turns_taken"], red["lands_played"], red["result"]) == (1, 1, "won")
    assert set(red) == {"seat", "name", "result", "final_life", "life_by_turn", "turns_taken", "spells_cast",
                        "lands_played", "unsupported_cards"}
