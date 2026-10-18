"""The decision inspector on the match page (#74)."""

from urllib.parse import urlencode

import pytest

from playtest.models import DecisionTrace, MatchEvent

from .factories import make_match

pytestmark = pytest.mark.django_db


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="owner", password="pw")


@pytest.fixture
def client(client, user):
    client.force_login(user)
    return client


def _trace(match, **fields):
    defaults = {
        "seat_position": 0, "turn": 2, "step": "MAIN1", "kind": "priority", "prompt": "Turn 2, MAIN1: you have priority",
        "options": [{"index": 0, "label": "Pass priority", "facts": []}, {"index": 1, "label": "Cast Shock #6", "facts": ["deals 2"]}],
        "probabilities": {"0": 0.2, "1": 0.8}, "choice": 1, "confidence": 0.7, "outcome": "done",
        "state": {"you": {"life": 20, "hand": [{"name": "Shock", "text": "Shock deals 2 damage to any target."}], "battlefield": []},
                  "opponents": [{"name": "Deck 1", "life": 18, "cards_in_hand": 5, "battlefield": [{"name": "Bear", "power": 2, "toughness": 2, "tapped": True}]}],
                  "stack": [], "active_player": "you", "turn": 2, "phase": "MAIN1"},
    }
    defaults.update(fields)
    return DecisionTrace.objects.create(match=match, **defaults)


def _url(match, **query):
    return f"/playtests/{match.playtest_id}/matches/{match.id}?" + urlencode({"tab": "decisions", **query})


def _detail(match, trace, query=""):
    return f"/playtests/{match.playtest_id}/matches/{match.id}/decisions/{trace.id}{query}"


def test_the_decisions_tab_lists_the_traces(client, user):
    match = make_match(user, status="finished")
    _trace(match)
    _trace(match, seat_position=1, choice=None, fallback_kind="low_confidence", outcome="delegated")
    page = client.get(_url(match)).content.decode()
    assert "Cast Shock #6" in page and "low_confidence" in page and "0.70" in page


def test_the_filters(client, user):
    match = make_match(user, status="finished")
    _trace(match, prompt="first")
    _trace(match, seat_position=1, kind="attack", options=[{"index": 0, "label": "Done (no more attackers)", "facts": []}], choice=0,
           flags=[{"check": "ignored_lethal", "reason": "x"}])
    _trace(match, choice=None, fallback_kind="error", outcome="delegated", options=[{"index": 0, "label": "Mulligan", "facts": []}], kind="mulligan")
    assert "Done (no more attackers)" not in client.get(_url(match, seat="1")).content.decode()
    flagged = client.get(_url(match, only="flagged")).content.decode()
    assert "Done (no more attackers)" in flagged and "Cast Shock #6" not in flagged
    assert "Cast Shock #6" not in client.get(_url(match, only="fallbacks")).content.decode()
    assert "Cast Shock #6" not in client.get(_url(match, kind="attack")).content.decode()


def test_a_match_without_traces_says_so(client, user):
    match = make_match(user, status="finished")
    assert "No decision traces" in client.get(_url(match)).content.decode()


def test_a_logged_decision_links_to_its_trace(client, user):
    match = make_match(user, status="finished")
    event = MatchEvent.objects.create(match=match, seq=0, turn=2, seat_position=0, kind="decision", text="priority: Cast Shock #6",
                                      payload={"kind": "priority", "choice": 1})
    trace = _trace(match, event=event)
    event.payload["trace"] = trace.id
    event.save()
    page = client.get(f"/playtests/{match.playtest_id}/matches/{match.id}?tab=log").content.decode()
    assert _detail(match, trace) in page


def test_the_detail_shows_what_jev_saw_and_answered(client, user):
    match = make_match(user, status="finished")
    trace = _trace(match, flags=[{"check": "pass_with_plays", "reason": "passed with 1 spell castable"}], instructions="You play Deck 0")
    page = client.get(_detail(match, trace)).content.decode()
    for text in ("Turn 2, MAIN1: you have priority", "Cast Shock #6", "deals 2", "0.80", "Pass priority",
                 "Shock deals 2 damage to any target.", "Bear", "18 life", "pass with plays left", "passed with 1 spell castable",
                 "You play Deck 0"):
        assert text in page, text


def test_previous_and_next_follow_the_filters(client, user):
    match = make_match(user, status="finished")
    first, other, last = _trace(match), _trace(match, seat_position=1), _trace(match)
    page = client.get(_detail(match, first, "?seat=1")).content.decode()
    assert _detail(match, last, "?seat=1") in page and _detail(match, other) not in page


def test_someone_elses_trace_is_a_404(client, user, django_user_model):
    mine = make_match(user, status="finished")
    theirs = make_match(django_user_model.objects.create_user(username="other"), status="finished")
    trace = _trace(theirs)
    assert client.get(_detail(theirs, trace)).status_code == 404
    assert client.get(f"/playtests/{mine.playtest_id}/matches/{mine.id}/decisions/{trace.id}").status_code == 404


def test_the_disagree_filter(client, user):
    match = make_match(user, status="finished")
    _trace(match, forge_ai_choice=1)
    _trace(match, forge_ai_choice=0, options=[{"index": 0, "label": "Pass priority", "facts": []}, {"index": 1, "label": "Play Mountain #9", "facts": []}])
    page = client.get(_url(match, only="disagree")).content.decode()
    assert "Play Mountain #9" in page and "Cast Shock #6" not in page


def test_the_disagree_filter_is_offered_only_with_forge_ais_choices(client, user):
    match = make_match(user, status="finished")
    _trace(match)
    assert "Disagrees with Forge AI" not in client.get(_url(match)).content.decode()
    _trace(match, forge_ai_choice=0)
    assert "Disagrees with Forge AI" in client.get(_url(match)).content.decode()


def test_a_pick_that_never_reached_the_game_counts_as_a_fallback(client, user):
    match = make_match(user, status="finished")
    _trace(match, outcome="delegated", fallback_reason="the answer did not reach the game")
    assert "Cast Shock #6" in client.get(_url(match, only="fallbacks")).content.decode()


def test_a_trace_without_scores_renders(client, user):
    match = make_match(user)
    trace = _trace(match)
    page = client.get(_detail(match, trace)).content.decode()
    assert page.count("Cast Shock #6") >= 1 and "score" not in page.lower().split("what jev saw")[0].split("options")[-1]


def test_the_detail_shows_each_outcome_summary_and_score(client, user):
    from .fakeforge import sample_decision

    match = make_match(user)
    decision = sample_decision(simulated=True)
    trace = _trace(match, kind="attack", prompt=decision["prompt"], options=decision["options"],
                   probabilities={"0": 0.2, "1": 0.7, "2": 0.1}, scores={"0": 3.0, "1": 4.2, "2": 3.4})
    page = client.get(_detail(match, trace)).content.decode()
    assert "Green: 20 → 17 life" in page and "score 4.2" in page and "no change" in page
