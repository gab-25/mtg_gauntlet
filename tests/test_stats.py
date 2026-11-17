"""The numbers the pages show, computed from stored matches."""

from datetime import timedelta

import pytest
from django.utils import timezone

from playtest import stats
from playtest.models import Match

from .factories import make_deck, make_match, make_playtest


def _done(user, playtest, winner, turns=8, opponent="Opp"):
    match = make_match(user, playtest=playtest, seats=(playtest.deck_name, opponent),
                       status=Match.Status.FINISHED, turn=turns)
    if winner is not None:
        match.winner = match.seats.get(position=winner)
    match.summary = {"turns": turns, "seats": [{"seat": 0}, {"seat": 1}]}
    match.save()
    return match


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="u")


@pytest.mark.django_db
def test_record_counts_finished_matches_from_seat_zero(user):
    playtest = make_playtest(user)
    matches = [_done(user, playtest, 0, turns=8), _done(user, playtest, 1, turns=10),
               _done(user, playtest, None, turns=12),
               make_match(user, playtest=playtest, status=Match.Status.RUNNING)]
    result = stats.record(matches)
    assert (result.won, result.lost, result.draw, result.played) == (1, 1, 1, 3)
    assert result.score == "1–1–1" and result.win_rate == 33
    assert result.avg_turns == 10.0


@pytest.mark.django_db
def test_record_of_nothing_finished_has_no_rates(user):
    result = stats.record([make_match(user, status=Match.Status.FAILED)])
    assert result.played == 0 and result.win_rate is None and result.avg_turns is None
    assert result.score == "0–0–0"


@pytest.mark.django_db
def test_a_match_without_a_summary_still_counts(user):
    match = make_match(user, status=Match.Status.FINISHED, turn=7)
    result = stats.record([match])
    assert result.draw == 1 and result.avg_turns == 7.0


@pytest.mark.django_db
def test_opponents_are_grouped_by_deck_then_by_name(user):
    rival = make_deck(user, name="Rival", fmt="duel")
    playtest = make_playtest(user)
    a = _done(user, playtest, 0, opponent="Rival")
    b = _done(user, playtest, 1, opponent="Rival")
    for match in (a, b):
        match.seats.filter(position=1).update(deck=rival)
    gone = _done(user, playtest, 0, opponent="Gone Archetype")
    rows = stats.record_by_opponent([a, b, gone])
    assert [(r.name, r.source, r.record.score) for r in rows] == [
        ("Rival", "Your deck", "1–1–0"), ("Gone Archetype", "Removed deck", "1–0–0")]


@pytest.mark.django_db
def test_needs_attention_lists_unchecked_unknown_importing_and_stale(user):
    now = timezone.now()
    make_deck(user, name="Unchecked", fmt="modern")
    make_deck(user, name="Odd", fmt="modern", coverage={"unresolved": ["X"], "unsupported_cards": []})
    make_deck(user, name="Slow", fmt="modern", status="processing")
    make_deck(user, name="Broken", fmt="modern", status="failed", error="No such card")
    make_deck(user, name="Fine", fmt="pioneer", coverage={"unresolved": [], "unsupported_cards": []})
    from decks.models import Deck

    notices = stats.needs_attention(list(Deck.objects.filter(owner=user)),
                                    {"modern": now - timedelta(days=9), "pioneer": now}, now)
    titles = [n.title for n in notices]
    assert "Unchecked is not checked" in titles and "Odd has cards Forge does not know" in titles
    assert "Slow is still importing" in titles and "Broken could not be imported" in titles
    assert "Modern metagame is 9 days old" in titles
    assert not any("Fine" in t or "Pioneer" in t for t in titles)
