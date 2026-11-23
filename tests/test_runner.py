"""Tests for running a stored match on the Forge bridge (a FakeForge, no network)."""

import pytest

from playtest import runner
from playtest.forge import ForgeDeckError
from playtest.models import Match, Seat

from .factories import make_constructed_deck, make_deck, make_playtest

pytestmark = pytest.mark.django_db


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="u")


def _match(user, fmt="modern", seed=5):
    match = Match.objects.create(owner=user, playtest=make_playtest(user, fmt=fmt, seed=seed), format=fmt, seed=seed)
    for position in (0, 1):
        deck = make_constructed_deck(user, name=f"Deck {position}", fmt=fmt) if fmt != "duel" else make_deck(user, name=f"Deck {position}", fmt=fmt)
        Seat.objects.create(match=match, position=position, deck=deck, deck_name=deck.name)
    return match


def _play(match):
    runner.run_match(match.id)
    match.refresh_from_db()
    return match


def test_a_duel_plays_to_the_end_and_stores_the_log(user, fake_forge):
    match = _play(_match(user))
    assert match.status == Match.Status.FINISHED, match.error
    assert match.forge_game_id and match.started_at and match.finished_at
    events = list(match.events.all())
    assert [e.seq for e in events] == list(range(len(events)))
    assert events[-1].kind == "game_over"
    assert {e.kind for e in events} == {"log", "game_over"}
    assert match.turn == fake_forge.games[match.forge_game_id].turn
    assert match.end_reason in ("AllOpponentsLost", "turn_limit")
    if match.end_reason == "AllOpponentsLost":
        assert match.winner.position == fake_forge.games[match.forge_game_id].winner
    assert [seat.life for seat in match.seats.all()] == fake_forge.games[match.forge_game_id].life
    assert match.summary["turns"] == match.turn


def test_each_seats_life_follows_the_game_while_it_runs(user, fake_forge, monkeypatch):
    seen = []
    store = runner._store

    def spy(match, events):
        store(match, events)
        lives = list(match.seats.values_list("life", flat=True))
        seen.append((Match.objects.get(pk=match.pk).turn, lives))

    monkeypatch.setattr(runner, "_store", spy)
    match = _play(_match(user))
    assert seen[0][1] == [20, 20]
    final = fake_forge.games[match.forge_game_id].life
    assert any(lives != [20, 20] for _, lives in seen[:-1]) and seen[-1][1] == final


def test_the_bridge_receives_the_decklists_and_the_seed(user, fake_forge):
    _play(_match(user, seed=11))
    [created] = fake_forge.created
    assert created["format"] == "modern" and created["seed"] == 11
    # Forge's AI plays every seat: no controller to name.
    assert all(set(seat) == {"name", "decklist"} for seat in created["seats"])
    assert created["seats"][0]["decklist"].startswith("Deck\n4 Spell 0")
    assert "Sideboard\n3 Side 0" in created["seats"][0]["decklist"]


def test_the_same_seed_replays_the_same_game(user, fake_forge):
    first = _play(_match(user, seed=9))
    second = _play(_match(user, seed=9))
    texts = lambda match: list(match.events.values_list("text", flat=True))  # noqa: E731
    assert texts(first) == texts(second)


def test_a_duel_commander_match_sends_the_commander(user, fake_forge):
    match = _play(_match(user, fmt="duel"))
    assert match.status == Match.Status.FINISHED, match.error
    assert fake_forge.created[0]["format"] == "duel_commander"
    assert fake_forge.created[0]["seats"][0]["decklist"].startswith("Commander\n1 Bear Lord\n")


def test_unsupported_cards_are_the_seats_coverage(user, fake_forge):
    fake_forge.unsupported = {"Side 0", "Mountain"}
    match = _play(_match(user))
    assert [s.coverage for s in match.seats.all()] == [{"unsupported_cards": ["Mountain", "Side 0"]}] * 2


def test_a_deck_with_an_unknown_card_never_starts(user, fake_forge):
    fake_forge.unknown = {"Spell 3"}
    match = _match(user)
    with pytest.raises(ForgeDeckError) as error:
        runner.start_game(match)
    assert error.value.unresolved == {"Deck 0": ["Spell 3"], "Deck 1": ["Spell 3"]}
    match.refresh_from_db()
    assert not match.forge_game_id and not fake_forge.games


def test_a_match_whose_deck_was_deleted_fails_cleanly(user, fake_forge):
    match = _match(user)
    match.seats.get(position=1).deck.delete()
    match = _play(match)
    assert match.status == Match.Status.FAILED
    assert "deleted" in match.error


def test_the_same_deck_twice_plays_under_two_names(user, fake_forge):
    match = _match(user)
    Seat.objects.filter(match=match).update(deck_name="Twins")
    _play(match)
    assert [s["name"] for s in fake_forge.created[0]["seats"]] == ["Twins (seat 1)", "Twins (seat 2)"]


def test_a_stopped_game_is_recorded_as_stopped(user, fake_forge, monkeypatch):
    match = _match(user)
    store = runner._store

    def stop_after_first(match, events):
        store(match, events)
        fake_forge.abort(match.forge_game_id)

    monkeypatch.setattr(runner, "_store", stop_after_first)
    match = _play(match)
    assert match.status == Match.Status.STOPPED
    assert match.get_end_reason_display() == "Stopped"


# -- failures end the game on the bridge -----------------------------------------------------


def test_a_game_the_bridge_forgot_fails_the_match_at_once(user, fake_forge, monkeypatch):
    """A restarted bridge has no game to come back: no minute of retries."""

    def retried(seconds):
        raise AssertionError("the runner waited for a game the bridge does not know")

    monkeypatch.setattr(runner, "MAX_OUTAGE_SECONDS", 3600)
    monkeypatch.setattr(runner.time, "sleep", retried)
    match = _match(user)
    runner.start_game(match)
    del fake_forge.games[match.forge_game_id]
    match = _play(match)
    assert match.status == Match.Status.FAILED
    assert match.error == "The Forge bridge no longer knows this game (it restarted?)."


def test_a_failed_match_aborts_its_game_so_forge_stops_playing_it(user, fake_forge, monkeypatch):
    from playtest import forge

    monkeypatch.setattr(runner, "MAX_OUTAGE_SECONDS", 0)
    monkeypatch.setattr(runner, "POLL_SECONDS", 0)
    match = _match(user)
    runner.start_game(match)

    def down(*args, **kwargs):
        raise forge.ForgeError("bridge gone")

    monkeypatch.setattr(fake_forge, "events", down)
    match = _play(match)
    assert match.status == Match.Status.FAILED and "bridge gone" in match.error
    assert fake_forge.games[match.forge_game_id].status == "aborted"


def test_a_game_that_ended_is_recorded_even_if_the_last_read_fails(user, fake_forge, monkeypatch):
    from playtest import forge

    match = _match(user)
    original, calls = fake_forge.events, []

    def flaky(game_id, since=0):
        if fake_forge.games[game_id].status != "running":
            calls.append(since)
            if len(calls) == 2:
                raise forge.ForgeError("blip")
        return original(game_id, since)

    monkeypatch.setattr(fake_forge, "events", flaky)
    match = _play(match)
    assert match.status == Match.Status.FINISHED, match.error


def test_a_match_deleted_before_its_job_starts_is_ignored(user, fake_forge):
    match = _match(user)
    match_id = match.id
    match.delete()
    runner.run_match(match_id)  # no error
