"""Tests for running a stored match on the Forge bridge (a FakeForge, no network)."""

import pytest

from playtest import runner, workers
from playtest.agents import typesafe
from playtest.forge import ForgeDeckError
from playtest.models import Match, MatchEvent, Seat

from .factories import make_constructed_deck, make_deck, make_playtest
from .test_jev_agent import StubJev

pytestmark = pytest.mark.django_db


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="u")


@pytest.fixture(autouse=True)
def jev(monkeypatch):
    """Every Jev seat answers through a confident stub: deterministic, no network."""
    stub = StubJev(confidence=0.9)
    monkeypatch.setattr(typesafe, "system_one", stub)
    return stub


def _match(user, agents=("jev", "jev"), fmt="modern", seed=5):
    match = Match.objects.create(owner=user, playtest=make_playtest(user, fmt=fmt, seed=seed), format=fmt, seed=seed)
    for position, agent in enumerate(agents):
        deck = make_constructed_deck(user, name=f"Deck {position}", fmt=fmt) if fmt != "duel" else make_deck(user, name=f"Deck {position}", fmt=fmt)
        Seat.objects.create(match=match, position=position, deck=deck, deck_name=deck.name, agent=agent)
    return match


def _play(match):
    runner.run_match(match.id)
    match.refresh_from_db()
    return match


def test_a_jev_duel_plays_to_the_end_and_stores_the_log(user, fake_forge):
    match = _play(_match(user))
    assert match.status == Match.Status.FINISHED, match.error
    assert match.forge_game_id and match.started_at and match.finished_at
    events = list(match.events.all())
    assert [e.seq for e in events] == list(range(len(events)))
    assert events[-1].kind == "game_over"
    assert {"log", "decision", "forge_ai"} <= {e.kind for e in events}
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


def test_the_bridge_receives_the_decklists_seed_and_controllers(user, fake_forge):
    _play(_match(user, agents=("jev", "forge_ai"), seed=11))
    [created] = fake_forge.created
    assert created["format"] == "modern" and created["seed"] == 11
    assert [s["controller"] for s in created["seats"]] == ["external", "forge_ai"]
    assert created["seats"][0]["decklist"].startswith("Deck\n4 Spell 0")
    assert "Sideboard\n3 Side 0" in created["seats"][0]["decklist"]


def test_jev_against_forge_ai_only_asks_the_jev_seat(user, fake_forge):
    match = _play(_match(user, agents=("jev", "forge_ai")))
    assert match.status == Match.Status.FINISHED, match.error
    assert set(match.events.filter(kind="decision").values_list("seat_position", flat=True)) == {0}


def test_the_same_seed_replays_the_same_game(user, fake_forge, jev):
    jev.prefer = lambda criteria: list(criteria)[-1]
    first = _play(_match(user, seed=9))
    second = _play(_match(user, seed=9))
    texts = lambda match: list(match.events.values_list("text", flat=True))  # noqa: E731
    assert texts(first) == texts(second)


@pytest.mark.django_db
def test_build_agent_tells_jev_its_deck_list(user):
    """As Argentum does: Jev knows what its deck holds, not where the cards are."""
    match = _match(user, agents=("jev", "forge_ai"))
    agent = workers.build_agent(match.seats.get(position=0))
    assert agent.deck.startswith("Deck (order unknown): {") and agent.deck.endswith(". Archetype: Deck 0")
    assert "Mountain=24" in agent.deck and "Spell 0=4" in agent.deck and "Side 0" not in agent.deck


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
    original = workers.serve

    def stop_after_first(client, decision, **kwargs):
        original(client, decision, **kwargs)
        client.abort(decision["game_id"])

    monkeypatch.setattr(workers, "serve", stop_after_first)
    match = _play(match)
    assert match.status == Match.Status.STOPPED
    assert match.get_end_reason_display() == "Stopped"


# -- Jev seats ---------------------------------------------------------------------------


def test_a_jev_match_stores_confidence_probabilities_and_model_on_every_decision(user, fake_forge, jev):
    match = _play(_match(user, agents=("jev", "jev")))
    assert match.status == Match.Status.FINISHED, match.error
    decisions = list(match.events.filter(kind="decision"))
    assert decisions and len(jev.requests) == len(decisions)
    for event in decisions:
        assert event.payload["confidence"] == 0.9 and event.payload["model"] == "typesafe/jev-1.13"
        assert event.payload["probabilities"][0]["p"] == 0.9
    summary = match.summary["seats"][0]
    assert summary["confidence"]["median"] == 0.9 and summary["decisions"]["agent"] > 0


def test_an_unsure_jev_still_decides(user, fake_forge, jev):
    """No confidence gate, as in Argentum: nothing is delegated, the confidence is only logged."""
    jev.confidence = 0.05
    match = _play(_match(user, agents=("jev", "forge_ai")))
    assert match.status == Match.Status.FINISHED, match.error
    decisions = list(match.events.filter(kind="decision", seat_position=0))
    assert decisions and not match.events.filter(kind="fallback", seat_position=0).exists()
    assert decisions[0].payload["confidence"] == 0.05
    assert match.summary["seats"][0]["confidence"]["median"] == 0.05


def test_an_unreachable_model_delegates_and_the_match_goes_on(user, fake_forge, jev):
    jev.fail = "network down"
    match = _play(_match(user, agents=("jev", "jev")))
    assert match.status == Match.Status.FINISHED, match.error
    payloads = [e.payload for e in match.events.filter(kind="fallback")]
    assert payloads and all(p["fallback_kind"] == "error" and "network down" in p["fallback_reason"] for p in payloads)


def test_the_call_budget_hands_the_rest_of_the_seats_game_to_forges_ai(user, fake_forge, jev, monkeypatch):
    monkeypatch.setenv("MAX_AGENT_CALLS", "3")
    match = _play(_match(user, agents=("jev", "jev")))
    assert len(jev.requests) == 6
    budget = match.events.filter(kind="fallback", payload__fallback_kind="budget")
    assert {e.seat_position for e in budget} == {0, 1}


def test_the_wall_clock_budget_hands_every_model_seat_over(user, fake_forge, jev, monkeypatch):
    monkeypatch.setenv("MAX_MATCH_SECONDS", "0")
    match = _play(_match(user))
    assert match.status == Match.Status.FINISHED, match.error
    assert not jev.requests and not match.events.filter(kind="decision").exists()
    budget = match.events.filter(kind="fallback", payload__fallback_kind="budget")
    assert {e.seat_position for e in budget} == {0, 1}


def test_an_agent_that_crashes_delegates(user, fake_forge, monkeypatch):
    class Broken:
        def choose(self, *args, **kwargs):
            raise RuntimeError("bug")

    monkeypatch.setattr(workers, "build_agent", lambda seat: Broken())
    match = _play(_match(user))
    assert match.status == Match.Status.FINISHED
    assert all("agent error: bug" in e.payload["fallback_reason"] for e in match.events.filter(kind="fallback"))


def test_a_play_forge_did_not_carry_out_is_not_offered_again(user, fake_forge, jev):
    """#71: Jev prefers a "Cast Stifle" that never reaches the stack; the same
    decision comes back, without it."""
    fake_forge.failing_cast = True
    match = _play(_match(user))
    assert match.status == Match.Status.FINISHED, match.error
    decisions = list(match.events.filter(kind="decision", payload__kind="priority"))
    failed = [e for e in decisions if "Cast Stifle" in e.text]
    assert failed
    for previous, event in zip(decisions, decisions[1:]):
        assert not (previous.seat_position == event.seat_position and previous.text == event.text)
    retries = [e for e in decisions if e.payload.get("failed_play")]
    assert len(retries) == len(failed)
    assert all("Cast Stifle" in e.payload["failed_play"] and "Cast Stifle" not in e.text for e in retries)
    seats = match.summary["seats"]
    assert sum(seat["failed_plays"] for seat in seats) == len(failed)
    assert not match.events.filter(kind="fallback").exists()


def test_a_decision_for_an_unknown_game_is_delegated(fake_forge):
    from .fakeforge import sample_decision

    decision = sample_decision()
    answer = workers.decide(decision, grace=0)
    assert answer.delegate and answer.fallback_reason == "no match for this game"


# -- the worker pool -----------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_ten_matches_started_together_finish_with_two_workers(user, fake_forge, monkeypatch):
    """Two workers serve the decisions of ten games at once. (The runners copy
    the logs afterwards: SQLite in memory does not take concurrent writers.)"""
    import time

    monkeypatch.setattr(workers, "LEASE_WAIT", 0.2)
    pool = workers.Pool(2, client_factory=lambda: fake_forge)
    matches = [_match(user, seed=seed) for seed in range(10)]
    for match in matches:
        runner.start_game(match)
    games = list(fake_forge.games.values())
    pool.start()
    try:
        deadline = time.monotonic() + 60
        while any(game.status == "running" for game in games) and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        pool.stop(timeout=5)
    assert [game.status for game in games] == ["finished"] * 10
    monkeypatch.setattr(workers, "_pool", pool)
    for match in matches:
        runner.run_match(match.id)
    statuses = list(Match.objects.values_list("status", flat=True))
    assert statuses == [Match.Status.FINISHED] * 10, list(Match.objects.values_list("error", flat=True))
    assert MatchEvent.objects.filter(kind="game_over").count() == 10


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


def test_a_failed_match_aborts_its_game_so_no_agent_keeps_playing(user, fake_forge, monkeypatch):
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


def test_a_decision_for_a_match_that_no_longer_runs_is_delegated(user, fake_forge):
    match = _match(user)
    runner.start_game(match)
    Match.objects.filter(pk=match.pk).update(status=Match.Status.FAILED)
    decision = fake_forge.lease(0, match.forge_game_id)
    answer = workers.decide(decision, grace=0)
    assert answer.delegate and answer.fallback_reason == "the match is failed"


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
