"""Exporting a match for an offline review (#76)."""

import json

import pytest
from django.core.management import CommandError, call_command

from playtest import export

from .test_traces import _played, jev, user  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db


def test_jsonl_is_every_event_in_order_with_its_trace(user, fake_forge, jev):
    match = _played(user)
    lines = [json.loads(line) for line in export.match_jsonl(match).splitlines()]
    assert [line["seq"] for line in lines] == list(match.events.values_list("seq", flat=True))
    decisions = [line for line in lines if line["kind"] == "decision"]
    assert decisions and all(line["trace"]["options"] and line["trace"]["state"] for line in decisions)
    assert all("trace" not in line for line in lines if line["kind"] == "log")


def test_jsonl_for_one_seat_and_only_flagged(user, fake_forge, jev):
    fake_forge.failing_cast = True
    match = _played(user)
    seat = [json.loads(line) for line in export.match_jsonl(match, seat=1).splitlines()]
    assert seat and all(line["seat"] in (1, None) for line in seat)
    flagged = [json.loads(line) for line in export.match_jsonl(match, flagged=True).splitlines()]
    assert flagged and all(line["trace"]["flags"] for line in flagged)


def test_markdown_narrates_each_turn_and_decision(user, fake_forge, jev):
    match = _played(user)
    text = export.match_markdown(match)
    assert text.startswith("# Deck 0 vs Deck 1")
    assert "## Turn 1" in text and "Keep this hand" in text and "→" in text


def test_a_match_without_traces_still_exports(user, fake_forge, jev, monkeypatch):
    monkeypatch.setenv("MAX_AGENT_CALLS", "0")
    match = _played(user)
    assert export.match_jsonl(match) and "## Turn 1" in export.match_markdown(match)


def test_the_commands_write_files(user, fake_forge, jev, tmp_path, capsys):
    match = _played(user)
    call_command("export_match", str(match.id), "--format", "md")
    assert "## Turn 1" in capsys.readouterr().out
    call_command("export_playtest", str(match.playtest_id), "--format", "jsonl", "--output", str(tmp_path))
    [written] = list(tmp_path.iterdir())
    assert written.name == export.file_name(match, "jsonl") and written.read_text()


def test_an_unknown_id_is_a_command_error():
    for command in ("export_match", "export_playtest"):
        with pytest.raises(CommandError):
            call_command(command, "not-a-uuid", "--output", "/nonexistent")


def _scored_trace(user):
    from playtest.models import DecisionTrace, MatchEvent

    from .factories import make_match
    from .fakeforge import sample_decision

    match = make_match(user)
    event = MatchEvent.objects.create(match=match, seq=1, turn=3, seat_position=0, kind="decision", text="attack: Attack with Bear",
                                      payload={"kind": "attack", "choice": 1})
    decision = sample_decision(simulated=True)
    trace = DecisionTrace.objects.create(
        match=match, seat_position=0, event=event, turn=3, step="COMBAT_DECLARE_ATTACKERS", kind="attack",
        prompt=decision["prompt"], state={"you": {"life": 20}, "opponents": []}, options=decision["options"],
        probabilities={"0": 0.2, "1": 0.7, "2": 0.1}, scores={"0": 3.0, "1": 4.2, "2": 3.4}, choice=1, confidence=0.6,
        outcome="done")
    return match, trace


def test_export_shows_summary_and_score(user):
    match, trace = _scored_trace(user)
    text = export.match_markdown(match)
    assert "→ Attack with Bear #3 (3/3) -> Player Green (20 life)" in text
    assert "`0.70`" in text and "`score 4.20`" in text and "— Green: 20 → 17 life" in text
    line = next(json.loads(l) for l in export.match_jsonl(match).splitlines() if json.loads(l)["kind"] == "decision")
    assert line["trace"]["options"][1]["score"] == 4.2 and line["trace"]["options"][1]["outcome"]["summary"] == "Green: 20 → 17 life"
    assert line["trace"]["options"][1]["p"] == 0.7


def test_a_partial_outcome_never_breaks_the_export(user):
    match, trace = _scored_trace(user)
    trace.options[0]["outcome"] = {"summary": "no change"}
    trace.options[1]["outcome"] = {"you": {}}
    trace.save()
    assert "Attack with Bear" in export.match_markdown(match)
