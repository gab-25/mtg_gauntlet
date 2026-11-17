"""Exporting a match for an offline review (#76)."""

import json

import pytest
from django.core.management import CommandError, call_command

from playtest import export, runner
from playtest.models import Match, Seat

from .factories import make_constructed_deck, make_playtest

pytestmark = pytest.mark.django_db


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="u")


def _played(user, seed=5):
    match = Match.objects.create(owner=user, playtest=make_playtest(user, fmt="modern", seed=seed), format="modern", seed=seed)
    for position in (0, 1):
        deck = make_constructed_deck(user, name=f"Deck {position}")
        Seat.objects.create(match=match, position=position, deck=deck, deck_name=deck.name)
    runner.run_match(match.id)
    match.refresh_from_db()
    return match


def test_jsonl_is_every_event_in_order(user, fake_forge):
    match = _played(user)
    lines = [json.loads(line) for line in export.match_jsonl(match).splitlines()]
    assert [line["seq"] for line in lines] == list(match.events.values_list("seq", flat=True))
    assert {line["kind"] for line in lines} == {"log", "game_over"}


def test_jsonl_for_one_seat_keeps_its_events_and_the_table_wide_ones(user, fake_forge):
    match = _played(user)
    match.events.filter(seq=0).update(seat_position=0)
    seat = [json.loads(line) for line in export.match_jsonl(match, seat=1).splitlines()]
    assert seat and all(line["seat"] in (1, None) for line in seat)
    assert 0 not in [line["seq"] for line in seat]


def test_markdown_narrates_each_turn(user, fake_forge):
    match = _played(user)
    text = export.match_markdown(match)
    assert text.startswith("# Deck 0 vs Deck 1")
    assert "## Before the game" in text and "## Turn 1" in text
    # Forge's quiet categories stay out of the narration.
    assert "Add {R}" not in text


def test_the_commands_write_files(user, fake_forge, tmp_path, capsys):
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
