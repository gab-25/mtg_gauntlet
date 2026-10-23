"""Web tests for setting up and following a match."""

import re

import pytest

from playtest.models import Match, Playtest

from .factories import make_deck, make_match, make_playtest


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="owner", password="pw")


@pytest.fixture
def jev(monkeypatch):
    """Jev plays every seat: a key and a stand-in answer, no network."""
    from playtest.agents import typesafe

    from .test_jev_agent import StubJev

    stub = StubJev(confidence=0.9)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(typesafe, "system_one", stub)
    return stub


@pytest.fixture
def client(client, user, jev):
    client.force_login(user)
    return client


def _form(fmt, decks, **extra):
    """The new-playtest form: the first deck is tested against the others."""
    data = {"format": fmt, "seed": "7"}
    if decks:
        data["deck"] = str(decks[0].id)
        data["opponents"] = [str(deck.id) for deck in decks[1:]]
    data.update(extra)
    return data


@pytest.mark.django_db
def test_new_match_form_lists_my_ready_decks(client, user):
    make_deck(user, name="Ready Bears", fmt="duel")
    make_deck(user, name="Still Importing", fmt="duel", status="processing")
    content = client.get("/playtests/new").content
    assert b"Ready Bears" in content
    assert b"Still Importing" not in content


@pytest.mark.django_db
def test_new_match_form_prefills_the_deck_from_its_page(client, user):
    deck = make_deck(user, fmt="duel")
    content = client.get(f"/playtests/new?deck={deck.id}").content.decode()
    assert f'value="{deck.id}" checked' in content
    assert 'name="format" value="duel"' in content


@pytest.mark.django_db
def test_a_deck_is_tested_against_each_opponent(client, user, fake_forge):
    tested, *opponents = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(3)]
    response = client.post("/playtests/create", _form("duel", [tested, *opponents]))
    playtest = Playtest.objects.get(owner=user)
    assert response.status_code == 302
    assert response["Location"] == f"/playtests/{playtest.id}"
    assert playtest.deck == tested and playtest.seed == 7
    matches = list(playtest.matches.order_by("seed"))
    assert [m.seed for m in matches] == [7, 8]
    for match, opponent in zip(matches, opponents):
        assert match.status == Match.Status.FINISHED, match.error
        assert [(s.position, s.deck_id, s.agent) for s in match.seats.all()] == [(0, tested.id, "jev"), (1, opponent.id, "jev")]
    assert [g["seed"] for g in fake_forge.created] == [7, 8]

    page = client.get(f"/playtests/{playtest.id}").content.decode()
    assert "vs" in page and "Deck 1" in page and "Deck 2" in page
    assert f"/playtests/{playtest.id}/matches/{matches[0].id}" in page
    # Every match ended: the session no longer polls.
    assert b"hx-trigger" not in client.get(f"/playtests/{playtest.id}/live").content

    log = client.get(f"/playtests/{playtest.id}/matches/{matches[0].id}?tab=log").content.decode()
    assert "Game log" in log and "Turn 1 (Deck 0)" in log
    assert f'href="/playtests/{playtest.id}"' in log.split('aria-label="Breadcrumb"', 1)[1]
    # Every step of every turn stays out of the page.
    assert "{T}: Add {R}" not in log


@pytest.mark.django_db
def test_multiplayer_commander_cannot_be_played_yet(client, user, fake_forge):
    decks = [make_deck(user, name=f"Deck {i}") for i in range(2)]
    response = client.post("/playtests/create", _form("commander", decks))
    assert response.status_code == 422
    assert b"Commander cannot be played yet" in response.content
    assert not Match.objects.exists() and not fake_forge.created
    page = client.get(f"/playtests/new?deck={decks[0].id}").content.decode()
    assert "Commander cannot be played yet" in page and "disabled" in page


@pytest.mark.django_db
def test_the_same_deck_can_sit_twice(client, user, fake_forge):
    deck = make_deck(user, fmt="duel")
    client.post("/playtests/create", _form("duel", [deck, deck]))
    match = Match.objects.get(owner=user)
    assert match.seats.count() == 2
    # Forge tells the two apart.
    assert [s["name"] for s in fake_forge.created[0]["seats"]] == ["Bears (seat 1)", "Bears (seat 2)"]


@pytest.mark.django_db
def test_a_playtest_needs_a_deck_and_at_least_one_opponent(client, user):
    deck = make_deck(user, name="Deck 0", fmt="duel")
    response = client.post("/playtests/create", _form("duel", [deck]))
    assert response.status_code == 422 and b"Pick at least one opponent" in response.content
    response = client.post("/playtests/create", {"format": "duel", "opponents": [str(deck.id)]})
    assert response.status_code == 422 and b"Pick the deck to test" in response.content
    assert not Playtest.objects.exists() and not Match.objects.exists()


@pytest.mark.django_db
def test_a_playtest_has_at_most_ten_opponents(client, user):
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(12)]
    response = client.post("/playtests/create", _form("duel", decks))
    assert response.status_code == 422 and b"at most 10 opponents; 11 chosen" in response.content


@pytest.mark.django_db
def test_decks_must_match_the_format(client, user):
    decks = [make_deck(user, name="Pod deck"), make_deck(user, name="Duel deck", fmt="duel")]
    response = client.post("/playtests/create", _form("duel", decks))
    assert response.status_code == 422
    assert b"Pod deck is not a Duel Commander deck" in response.content


@pytest.mark.django_db
def test_someone_elses_deck_cannot_be_tested_or_played_against(client, user, django_user_model):
    theirs = make_deck(django_user_model.objects.create_user(username="other"), fmt="duel")
    mine = make_deck(user, fmt="duel")
    response = client.post("/playtests/create", _form("duel", [mine, theirs]))
    assert response.status_code == 422 and b"Pick opponents among your imported decks" in response.content
    response = client.post("/playtests/create", _form("duel", [theirs, mine]))
    assert response.status_code == 422 and b"Pick the deck to test" in response.content


@pytest.mark.django_db
def test_a_jev_playtest_needs_an_api_key(client, user, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY")
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    response = client.post("/playtests/create", _form("duel", decks))
    assert response.status_code == 422
    assert b"OPENROUTER_API_KEY" in response.content
    assert b"no Jev playtest can start" in client.get("/playtests/new").content
    # The warning is Jev's: with Forge AI picked the form says nothing of the key.
    assert b"OPENROUTER_API_KEY" not in client.get("/playtests/new?agent=forge_ai").content


@pytest.mark.django_db
def test_a_deck_forge_does_not_know_is_refused_naming_the_card(client, user, fake_forge):
    tested = make_deck(user, name="Deck 0", fmt="duel")
    good = make_deck(user, name="Deck 1", fmt="duel")
    odd = make_deck(user, name="Odd", fmt="duel")
    odd.cards[2]["data"]["name"] = "Nope Card"
    odd.save()
    fake_forge.unknown = {"Nope Card"}
    response = client.post("/playtests/create", _form("duel", [tested, good, odd]))
    assert response.status_code == 422
    assert "Odd: Forge does not know Nope Card." in response.content.decode()
    # The whole session is refused, and the game already started is ended.
    assert not Playtest.objects.exists() and not Match.objects.exists()
    assert [g.status for g in fake_forge.games.values()] == ["aborted"]


@pytest.mark.django_db
def test_an_unreachable_bridge_is_reported_and_no_match_is_left(client, user, monkeypatch):
    from playtest import forge

    def down(*args, **kwargs):
        raise forge.ForgeError("Forge bridge unreachable at http://localhost:8080: refused")

    monkeypatch.setattr(forge.ForgeClient, "create_game", down)
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    response = client.post("/playtests/create", _form("duel", decks))
    assert response.status_code == 503
    assert b"Forge bridge unreachable" in response.content
    assert not Match.objects.exists()


@pytest.mark.django_db
def test_bad_numbers_are_reported(client, user):
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    response = client.post("/playtests/create", _form("duel", decks, seed="abc"))
    assert response.status_code == 422
    assert b"seed must be a whole number" in response.content


@pytest.mark.django_db
def test_someone_elses_match_is_a_404(client, django_user_model):
    other = django_user_model.objects.create_user(username="other")
    match = make_match(other)
    assert client.get(f"/playtests/{match.playtest_id}/matches/{match.id}").status_code == 404
    assert client.get(f"/playtests/{match.playtest_id}/matches/{match.id}/live").status_code == 404
    assert client.post(f"/playtests/{match.playtest_id}/matches/{match.id}/stop").status_code == 404
    assert client.post(f"/playtests/{match.playtest_id}/matches/{match.id}/delete").status_code == 404


@pytest.mark.django_db
def test_a_running_match_polls_its_live_region_and_can_be_stopped(client, user, fake_forge):
    game = fake_forge.create_game("duel", 1, [{"name": "A", "decklist": "1 Forest"}, {"name": "B", "decklist": "1 Forest"}])
    match = make_match(user, status=Match.Status.RUNNING, forge_game_id=game["id"])
    page = client.get(f"/playtests/{match.playtest_id}/matches/{match.id}").content
    assert b"hx-trigger" in page and b"Stop" in page
    assert client.post(f"/playtests/{match.playtest_id}/matches/{match.id}/stop").status_code == 302
    assert fake_forge.games[game["id"]].status == "aborted"


@pytest.mark.django_db
def test_deleting_a_running_match_aborts_its_game(client, user, fake_forge):
    game = fake_forge.create_game("duel", 1, [{"name": "A", "decklist": "1 Forest"}, {"name": "B", "decklist": "1 Forest"}])
    match = make_match(user, status=Match.Status.RUNNING, forge_game_id=game["id"])
    assert client.post(f"/playtests/{match.playtest_id}/matches/{match.id}/delete").status_code == 302
    assert not Match.objects.exists()
    assert fake_forge.games[game["id"]].status == "aborted"


# -- coverage, confidence and the summary ---------------------------------------------


@pytest.mark.django_db
def test_the_form_offers_no_agent_choice(client):
    content = client.get("/playtests/new").content.decode()
    assert 'name="seat-0-agent"' not in content
    assert "Jev on every seat" in content


@pytest.mark.django_db
def test_a_posted_agent_is_ignored_every_seat_is_jev(client, user, fake_forge):
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    client.post("/playtests/create", _form("duel", decks, **{"seat-0-agent": "forge_ai", "seat-1-agent": "random"}))
    match = Match.objects.get(owner=user)
    assert list(match.seats.values_list("agent", flat=True)) == ["jev", "jev"]
    assert [s["controller"] for s in fake_forge.created[0]["seats"]] == ["external", "external"]


def _finished(user, **seat_fields):
    from playtest.models import MatchEvent, Seat

    match = make_match(user, status=Match.Status.FINISHED, turn=3, end_reason="turn_limit")
    if seat_fields:
        Seat.objects.filter(match=match, position=0).update(**seat_fields)
    return match, MatchEvent


@pytest.mark.django_db
def test_seat_cards_show_forges_coverage(client, user):
    match, _ = _finished(user, coverage={"unsupported_cards": ["Odd Card"]})
    match.seats.filter(position=1).update(coverage={"unsupported_cards": []})
    page = client.get(f"/playtests/{match.playtest_id}/matches/{match.id}").content.decode()
    assert "1 unsupported" in page and "Odd Card" in page
    assert "Forge plays every card of this deck" in page
    assert "Turn limit reached after 3 turns" in page


@pytest.mark.django_db
def test_the_log_shows_confidence_fallback_reasons_and_folds_forge_ai_decisions(client, user):
    match, MatchEvent = _finished(user)
    probabilities = [{"label": "Attack with Bear", "p": 0.82}, {"label": "Pass priority", "p": 0.11}]
    rows = [
        ("decision", 0, "attack: Attack with Bear", {"kind": "attack", "choice": 1, "confidence": 0.82, "probabilities": probabilities, "model": "typesafe/jev-1.13"}),
        ("fallback", 0, "block: decided by Forge AI (delegate)", {"kind": "block", "reason": "delegate", "fallback_kind": "low_confidence", "confidence": 0.11, "probabilities": probabilities}),
        ("forge_ai", 0, "declareBlockers: decided by Forge AI", {"method": "declareBlockers"}),
        ("fallback", 1, "priority: decided by Forge AI (timeout)", {"kind": "priority", "reason": "timeout"}),
        ("forge_ai", 1, "chooseSpellAbilityToPlay: decided by Forge AI", {"method": "chooseSpellAbilityToPlay"}),
        ("forge_ai", 0, "assignCombatDamage: decided by Forge AI", {"method": "assignCombatDamage"}),
        ("forge_ai", 0, "payManaCost: decided by Forge AI", {"method": "payManaCost"}),
        ("forge_ai", 0, "payManaCost: decided by Forge AI", {"method": "payManaCost"}),
        ("log", None, "Red's Untap step", {"type": "PHASE"}),
    ]
    for seq, (kind, seat, text, payload) in enumerate(rows):
        MatchEvent.objects.create(match=match, seq=seq, turn=1, seat_position=seat, kind=kind, text=text, payload=payload)
    page = client.get(f"/playtests/{match.playtest_id}/matches/{match.id}?tab=log").content.decode()
    assert "0.82" in page and "runner-up: Pass priority" in page
    assert "low_confidence" in page and "Jev leaned to: Attack with Bear" in page
    assert "timeout" in page
    assert "assignCombatDamage, payManaCost (3×)" in page
    # A fallback's own Forge-AI decision is not repeated.
    assert "chooseSpellAbilityToPlay" not in page and "declareBlockers" not in page
    assert "Untap step" not in page


def test_the_log_names_a_play_forge_did_not_carry_out(client, user):
    match, MatchEvent = _finished(user)
    stifle = "Cast Stifle #180 for {U}: Counter target activated or triggered ability."
    MatchEvent.objects.create(match=match, seq=0, turn=3, seat_position=0, kind="decision", text=f"priority: {stifle}",
                              payload={"kind": "priority", "choice": 2})
    MatchEvent.objects.create(match=match, seq=1, turn=3, seat_position=0, kind="decision", text="priority: Pass priority",
                              payload={"kind": "priority", "choice": 0, "failed_play": stifle})
    MatchEvent.objects.create(match=match, seq=2, turn=3, seat_position=0, kind="play_failed", text=f"{stifle}: not played (targets)",
                              payload={"ability": stifle, "reason": "targets"})
    page = client.get(f"/playtests/{match.playtest_id}/matches/{match.id}?tab=log").content.decode()
    assert "failed play" in page and "Forge did not carry out Cast Stifle #180" in page
    assert "Forge did not carry it out (targets)" in page


@pytest.mark.django_db
def test_a_finished_match_shows_its_summary(client, user, fake_forge):
    match, _ = _finished(user)
    assert "Match summary" not in client.get(f"/playtests/{match.playtest_id}/matches/{match.id}").content.decode()
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    client.post("/playtests/create", _form("duel", decks))
    played = Match.objects.exclude(pk=match.pk).get()
    page = client.get(f"/playtests/{played.playtest_id}/matches/{played.id}").content.decode()
    assert "Match summary" in page and "Spells cast" in page and "Fallbacks" in page and "Failed plays" in page
    assert "Automatic for every seat: mana payment" in page and "assignCombatDamage" in page
    assert "% Jev" in page


@pytest.mark.django_db
def test_a_constructed_match_between_two_jev_agents(client, user, fake_forge, monkeypatch):
    from playtest.agents import typesafe

    from .factories import make_constructed_deck
    from .test_jev_agent import StubJev

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(typesafe, "system_one", StubJev())
    decks = [make_constructed_deck(user, name=f"Goblins {i}") for i in range(2)]
    client.post("/playtests/create", _form("modern", decks))
    match = Match.objects.get(owner=user)
    assert match.status == Match.Status.FINISHED, match.error
    assert [s["controller"] for s in fake_forge.created[0]["seats"]] == ["external", "external"]
    assert set(match.events.filter(kind="decision").values_list("seat_position", flat=True)) == {0, 1}


@pytest.mark.django_db
def test_the_new_match_form_tags_decks_by_format(client, user):
    from .factories import make_constructed_deck

    tested = make_constructed_deck(user, name="Goblins")
    make_deck(user, name="Duel Bears", fmt="duel")
    page = client.get(f"/playtests/new?deck={tested.id}").content.decode()
    assert "Goblins" in page and "Modern" in page
    # Only decks of the tested deck's format are offered as opponents.
    opponents = page.split('id="opponents"', 1)[1]
    assert "Duel Bears" not in opponents
    assert "benchmark" not in page


def _metagame(fmt="modern", **fields):
    from decks.models import MetagameDeck

    from .factories import constructed_cards

    cards = constructed_cards(fmt)
    defaults = {"source": "mtgtop8", "archetype": "Goblins", "meta_share": 8.4, "rank": 1, "cards": cards,
                "coverage": {"unresolved": ["Odd Goblin"], "unsupported_cards": []}}
    defaults.update(fields)
    return MetagameDeck.objects.create(format=fmt, **defaults)


@pytest.mark.django_db
def test_the_form_offers_metagame_decks_warning_of_cards_forge_does_not_know(client):
    deck = _metagame()
    page = client.get("/playtests/new?format=modern").content.decode()
    assert f'name="opponents" value="metagame:{deck.id}"' in page
    assert "1 card unknown to Forge" in page
    assert "<span translate=\"no\">Goblins</span> has cards Forge does not know: its match will be refused." in page


@pytest.mark.django_db
def test_a_deck_tested_against_a_metagame_deck(client, user, fake_forge):
    from .factories import make_constructed_deck

    mine = make_constructed_deck(user)
    meta = _metagame(coverage={"unresolved": [], "unsupported_cards": []})
    data = {"format": "modern", "seed": "2", "deck": str(mine.id), "opponents": [f"metagame:{meta.id}"]}
    assert client.post("/playtests/create", data).status_code == 302
    match = Match.objects.get(owner=user)
    assert match.status == Match.Status.FINISHED, match.error
    seat = match.seats.get(position=1)
    assert seat.metagame_deck == meta and seat.deck_name == "Goblins"
    assert "Metagame · MTGTop8" in client.get(f"/playtests/{match.playtest_id}/matches/{match.id}").content.decode()
    assert "Metagame" in client.get(f"/playtests/{match.playtest_id}").content.decode()


@pytest.mark.django_db
def test_starting_a_playtest_keeps_forges_answer_on_an_unchecked_metagame_deck(client, user, fake_forge):
    from .factories import make_constructed_deck

    mine = make_constructed_deck(user)
    meta = _metagame(coverage={})
    data = {"format": "modern", "seed": "2", "deck": str(mine.id), "opponents": [f"metagame:{meta.id}"]}
    fake_forge.unknown = {"Spell 3"}
    assert client.post("/playtests/create", data).status_code == 422
    meta.refresh_from_db()
    assert meta.coverage == {"unresolved": ["Spell 3"], "unsupported_cards": []}

    fake_forge.unknown, fake_forge.unsupported = set(), {"Side 0"}
    assert client.post("/playtests/create", data).status_code == 302
    meta.refresh_from_db()
    assert meta.forge_checked and meta.coverage == {"unresolved": [], "unsupported_cards": ["Side 0"]}


@pytest.mark.django_db
def test_a_metagame_deck_of_another_format_or_failed_is_refused(client, user):
    from .factories import make_constructed_deck

    mine = make_constructed_deck(user)
    other = _metagame("pauper")
    broken = _metagame(status="failed", error="Missing Card")
    for value, message in ((f"metagame:{other.id}", b"not a Modern metagame deck"), (f"metagame:{broken.id}", b"could not be imported")):
        data = {"format": "modern", "deck": str(mine.id), "opponents": [value]}
        response = client.post("/playtests/create", data)
        assert response.status_code == 422 and message in response.content


@pytest.mark.django_db
def test_the_deck_page_checks_the_deck_with_forge_only_on_demand(client, user, fake_forge, monkeypatch):
    from playtest import forge

    deck = make_deck(user)
    called = []
    monkeypatch.setattr(fake_forge, "check_deck", lambda fmt, text: called.append((fmt, text)) or {"unresolved": ["Bear 3"], "unsupported_cards": [], "missing_commander": False})
    page = client.get(f"/decks/{deck.id}").content.decode()
    assert "Check with Forge" in page and not called
    result = client.post(f"/decks/{deck.id}/forge-check").content.decode()
    assert called[0][0] == "commander" and called[0][1].startswith("Commander\n1 Bear Lord")
    assert "a match with this deck would be refused" in result and "Bear 3" in result

    def down(*args):
        raise forge.ForgeError("Forge bridge unreachable at http://localhost:8080")

    monkeypatch.setattr(fake_forge, "check_deck", down)
    assert "Forge bridge unreachable" in client.post(f"/decks/{deck.id}/forge-check").content.decode()


@pytest.mark.django_db
def test_someone_elses_deck_cannot_be_checked(client, django_user_model, fake_forge):
    deck = make_deck(django_user_model.objects.create_user(username="other"))
    assert client.post(f"/decks/{deck.id}/forge-check").status_code == 404


@pytest.mark.django_db
def test_a_jev_match_on_the_fake_bridge_shows_confidence_and_fallbacks(client, user, fake_forge, monkeypatch):
    from playtest.agents import typesafe

    from .test_jev_agent import StubJev

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(typesafe, "system_one", StubJev(confidence=0.9))
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    client.post("/playtests/create", _form("duel", decks))
    match = Match.objects.get(owner=user)
    page = client.get(f"/playtests/{match.playtest_id}/matches/{match.id}?tab=log").content.decode()
    assert "0.90" in page and "runner-up:" in page

    monkeypatch.setattr(typesafe, "system_one", StubJev(fail="HTTP 503"))
    client.post("/playtests/create", _form("duel", decks))
    down = Match.objects.exclude(pk=match.pk).get()
    page = client.get(f"/playtests/{down.playtest_id}/matches/{down.id}?tab=log").content.decode()
    assert "decided by Forge AI (delegate)" in page and "error" in page and "HTTP 503" in page


@pytest.mark.django_db
def test_stopping_a_match_nobody_follows_any_more_ends_it(client, user, fake_forge):
    game = fake_forge.create_game("duel", 1, [{"name": "A", "decklist": "1 Forest"}, {"name": "B", "decklist": "1 Forest"}])
    match = make_match(user, status=Match.Status.RUNNING, forge_game_id=game["id"])
    client.post(f"/playtests/{match.playtest_id}/matches/{match.id}/stop")
    match.refresh_from_db()
    assert match.status == Match.Status.STOPPED and b"hx-trigger" not in client.get(f"/playtests/{match.playtest_id}/matches/{match.id}/live").content


@pytest.mark.django_db
def test_an_unreadable_bridge_answer_leaves_no_match(client, user, monkeypatch):
    from playtest import forge

    monkeypatch.setattr(forge.ForgeClient, "create_game", lambda *a, **k: {"status": "running"})
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    response = client.post("/playtests/create", _form("duel", decks))
    assert response.status_code == 503 and not Match.objects.exists()


@pytest.mark.django_db
def test_the_jev_model_cannot_be_set_from_the_form(client, user, fake_forge, jev):
    assert 'name="seat-0-model"' not in client.get("/playtests/new").content.decode()
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    client.post("/playtests/create", _form("duel", decks, **{"seat-0-model": "openai/gpt-5"}))
    match = Match.objects.get(owner=user)
    assert list(match.seats.values_list("model", flat=True)) == ["", ""]
    assert {model for _, _, model in jev.requests} == {"~typesafe/jev-latest"}


def _running_session(user, fake_forge):
    playtest = Playtest.objects.create(owner=user, format="duel", deck_name="Deck 0", seed=1)
    game = fake_forge.create_game("duel", 1, [{"name": "A", "decklist": "1 Forest"}, {"name": "B", "decklist": "1 Forest"}])
    match = Match.objects.create(owner=user, playtest=playtest, seed=1, status=Match.Status.RUNNING, forge_game_id=game["id"])
    return playtest, match, game["id"]


@pytest.mark.django_db
def test_a_running_session_polls_and_can_be_stopped(client, user, fake_forge):
    playtest, match, game_id = _running_session(user, fake_forge)
    page = client.get(f"/playtests/{playtest.id}").content
    assert b"hx-trigger" in page and b"Stop all" in page
    assert client.post(f"/playtests/{playtest.id}/stop").status_code == 302
    match.refresh_from_db()
    assert match.status == Match.Status.STOPPED and fake_forge.games[game_id].status == "aborted"


@pytest.mark.django_db
def test_deleting_a_session_aborts_its_games_and_deletes_its_matches(client, user, fake_forge):
    playtest, match, game_id = _running_session(user, fake_forge)
    assert client.post(f"/playtests/{playtest.id}/delete").status_code == 302
    assert not Playtest.objects.exists() and not Match.objects.exists()
    assert fake_forge.games[game_id].status == "aborted"


@pytest.mark.django_db
def test_someone_elses_session_is_a_404(client, django_user_model):
    playtest = Playtest.objects.create(owner=django_user_model.objects.create_user(username="other"), format="duel", deck_name="X", seed=1)
    for url in (f"/playtests/{playtest.id}", f"/playtests/{playtest.id}/live"):
        assert client.get(url).status_code == 404
    for url in (f"/playtests/{playtest.id}/stop", f"/playtests/{playtest.id}/delete"):
        assert client.post(url).status_code == 404


@pytest.mark.django_db
def test_sessions_are_listed_on_the_playtests_page_and_the_home_page(client, user, fake_forge):
    tested, opponent = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    client.post("/playtests/create", _form("duel", [tested, opponent]))
    playtest = Playtest.objects.get()
    for url in ("/playtests", "/"):
        page = client.get(url).content.decode()
        assert f"/playtests/{playtest.id}" in page and "Deck 1" in page


@pytest.mark.django_db
def test_a_match_is_found_only_under_its_own_playtest(client, user):
    match = make_match(user)
    other = make_playtest(user)
    assert client.get(f"/playtests/{other.id}/matches/{match.id}").status_code == 404
    assert client.post(f"/playtests/{other.id}/matches/{match.id}/stop").status_code == 404
    assert client.get(f"/playtests/{match.playtest_id}/matches/{match.id}").status_code == 200


@pytest.mark.django_db
def test_the_old_match_urls_are_gone(client, user):
    match = make_match(user)
    for url in ("/matches", "/matches/new", f"/matches/{match.id}"):
        assert client.get(url).status_code == 404


def _checked(page):
    return re.findall(r'name="opponents" value="([^"]+)" checked', page)


def _checkboxes(page):
    return re.findall(r'name="opponents" value="([^"]+)"', page)


@pytest.mark.django_db
def test_new_playtest_preselects_the_deck_and_its_format_opponents(client, user):
    from .factories import make_constructed_deck

    tested = make_constructed_deck(user, name="Tested")
    make_constructed_deck(user, name="Other Modern")
    make_deck(user, name="A Duel Deck", fmt="duel")
    _metagame("modern", archetype="Eldrazi Ramp", rank=1)
    page = client.get(f"/playtests/new?deck={tested.id}").content.decode()
    assert f'value="{tested.id}" checked' in page
    opponents = page.split('id="opponents"', 1)[1]
    assert "Eldrazi Ramp" in opponents and "Other Modern" in opponents and "A Duel Deck" not in opponents
    assert _checked(page) == []


@pytest.mark.django_db
def test_run_again_preselects_the_opponents(client, user):
    tested, rival = make_deck(user, name="T", fmt="duel"), make_deck(user, name="R", fmt="duel")
    page = client.get(f"/playtests/new?deck={tested.id}&opponents={rival.id}").content.decode()
    assert _checked(page) == [str(rival.id)]
    assert "Start 1 game" in page


@pytest.mark.django_db
def test_a_format_alone_preselects_the_top_five_metagame_decks(client):
    decks = [_metagame("modern", archetype=f"Deck {rank}", rank=rank) for rank in range(1, 8)]
    page = client.get("/playtests/new?format=modern").content.decode()
    assert _checked(page) == [f"metagame:{d.id}" for d in decks[:5]]


@pytest.mark.django_db
@pytest.mark.parametrize("query", ["?deck=not-a-uuid", "?deck=00000000-0000-0000-0000-000000000000",
                                   "?opponents=metagame:999&opponents=junk", "?format=nope"])
def test_a_dirty_query_string_is_ignored(client, query):
    response = client.get(f"/playtests/new{query}")
    assert response.status_code == 200 and _checked(response.content.decode()) == []


@pytest.mark.django_db
def test_someone_elses_deck_is_not_preselected(client, django_user_model):
    theirs = make_deck(django_user_model.objects.create_user(username="other"), fmt="duel")
    assert str(theirs.id) not in client.get(f"/playtests/new?deck={theirs.id}").content.decode()


@pytest.mark.django_db
def test_changing_deck_reloads_the_opponents(client, user):
    from .factories import make_constructed_deck

    tested = make_constructed_deck(user)
    _metagame("modern", archetype="Amulet Titan", rank=2)
    part = client.get(f"/playtests/new/opponents?deck={tested.id}").content.decode()
    assert "Amulet Titan" in part and 'id="opponents"' in part and 'hx-swap-oob="true"' in part


@pytest.mark.django_db
def test_the_playtest_list_filters_by_deck_and_format(client, user):
    a, b = make_deck(user, name="Alpha", fmt="duel"), make_deck(user, name="Beta", fmt="duel")
    make_match(user, playtest=make_playtest(user, deck=a, deck_name="Alpha"))
    make_match(user, playtest=make_playtest(user, deck=b, deck_name="Beta"))
    rows = client.get(f"/playtests?deck={a.id}").content.decode().split("<tbody>", 1)[1]
    assert "Alpha" in rows and "Beta" not in rows
    rows = client.get("/playtests?format=modern").content.decode()
    assert "No playtest matches" in rows
    assert client.get("/playtests?deck=junk&format=nope").status_code == 200


@pytest.mark.django_db
def test_the_playtest_page_shows_score_rows_and_run_again(client, user):
    from playtest.models import Seat

    tested, rival = make_deck(user, name="Tested", fmt="duel"), make_deck(user, name="Rival", fmt="duel")
    playtest = make_playtest(user, deck=tested, deck_name="Tested", seed=4417)
    match = make_match(user, playtest=playtest, seats=("Tested", "Rival"), status="finished", turn=9)
    Seat.objects.filter(match=match, position=0).update(life=14)
    Seat.objects.filter(match=match, position=1).update(deck=rival, life=0)
    match.winner = match.seats.get(position=0)
    match.save()
    page = client.get(f"/playtests/{playtest.id}").content.decode()
    assert "vs</span> the field" in page and "seed 4417" in page and "1–0–0" in page and "100%" in page
    assert "1 of 1 game done" in page and "Your deck" in page and "14 – 0" in page
    assert f"/playtests/new?deck={tested.id}&amp;games=1&amp;agent=jev&amp;opponents={rival.id}" in page
    assert f'href="/decks/{tested.id}"' in page.split('aria-label="Breadcrumb"', 1)[1]
    assert 'class="segment won"' in page


@pytest.mark.django_db
def test_a_playtest_of_a_deleted_deck_still_renders(client, user):
    playtest = make_playtest(user, deck_name="Gone")
    make_match(user, playtest=playtest, seats=("Gone", "Rival"))
    page = client.get(f"/playtests/{playtest.id}").content.decode()
    assert "Gone" in page and "Run again" not in page and "Removed deck" in page
    assert f'href="/playtests"' in page.split('aria-label="Breadcrumb"', 1)[1]


@pytest.mark.django_db
def test_the_match_page_has_summary_and_log_tabs_and_a_next_match(client, user):
    playtest = make_playtest(user)
    first = make_match(user, playtest=playtest, seed=1, status="finished")
    second = make_match(user, playtest=playtest, seed=2)
    page = client.get(f"/playtests/{playtest.id}/matches/{first.id}").content.decode()
    assert 'href="?tab=log"' in page and 'class="tab is-active" aria-current="page"' in page
    assert f'href="/playtests/{playtest.id}/matches/{second.id}"' in page and "Next match" in page
    assert "Next match" not in client.get(f"/playtests/{playtest.id}/matches/{second.id}").content.decode()


@pytest.mark.django_db
def test_the_log_filters_key_moments_and_fallbacks(client, user):
    match, MatchEvent = _finished(user)
    MatchEvent.objects.create(match=match, seq=0, turn=1, kind="log", text="Turn 1 (Deck 0)", payload={"type": "TURN"})
    MatchEvent.objects.create(match=match, seq=1, turn=1, seat_position=0, kind="decision", text="attack: X", payload={"confidence": 0.9})
    MatchEvent.objects.create(match=match, seq=2, turn=1, seat_position=0, kind="fallback", text="block: decided by Forge AI (timeout)", payload={"reason": "timeout"})
    base = f"/playtests/{match.playtest_id}/matches/{match.id}?tab=log"
    fallbacks = client.get(base + "&show=fallbacks").content.decode()
    assert "decided by Forge AI (timeout)" in fallbacks and "attack: X" not in fallbacks
    key = client.get(base + "&show=key").content.decode()
    assert "Turn 1 (Deck 0)" in key and "attack: X" not in key
    assert "attack: X" in client.get(base + "&show=nope").content.decode()


@pytest.mark.django_db
@pytest.mark.parametrize("query", ["", "?deck=junk", "?deck=00000000-0000-0000-0000-000000000000"])
def test_only_a_format_alone_preselects_the_metagame(client, query):
    from playtest.forms import DEFAULT_MATCH_FORMAT

    _metagame(DEFAULT_MATCH_FORMAT, archetype="Top Deck", rank=1)
    assert _checked(client.get(f"/playtests/new{query}").content.decode()) == []


@pytest.mark.django_db
@pytest.mark.parametrize("query", ["", "?deck=junk", "?format=nope"])
def test_without_a_deck_or_format_no_format_is_assumed(client, user, query):
    from playtest.forms import DEFAULT_MATCH_FORMAT

    make_deck(user, name="Mine", fmt=DEFAULT_MATCH_FORMAT)
    _metagame(DEFAULT_MATCH_FORMAT, archetype="Top Deck", rank=1)
    page = client.get(f"/playtests/new{query}").content.decode()
    assert "Pick the deck to test first" in page
    assert "Top Deck" not in page and _checkboxes(page) == []
    assert 'name="format" value=""' in page and "life</span>" not in page
    part = client.get(f"/playtests/new/opponents{query}").content.decode()
    assert "Pick the deck to test first" in part


@pytest.mark.django_db
def test_picking_a_deck_keeps_the_chosen_opponents_of_its_format(client, user):
    from .factories import make_constructed_deck

    tested = make_constructed_deck(user)
    meta = _metagame("modern", archetype="Amulet Titan", rank=1)
    other = _metagame("pauper", archetype="Faeries", rank=1)
    page = client.get("/playtests/new?format=modern").content.decode()
    assert 'hx-include="this, #opponents input[name=opponents]:checked, #games, input[name=agent]:checked"' in page
    part = client.get("/playtests/new/opponents", {"deck": tested.id, "opponents": [f"metagame:{meta.id}", f"metagame:{other.id}"]}).content.decode()
    assert _checked(part) == [f"metagame:{meta.id}"]


@pytest.mark.django_db
def test_each_match_of_a_session_is_queued_for_the_worker(client, user, fake_forge, monkeypatch):
    from playtest import tasks

    queued = []
    monkeypatch.setattr(tasks.run_match_task, "delay", queued.append)
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(3)]
    assert client.post("/playtests/create", _form("duel", decks)).status_code == 302
    playtest = Playtest.objects.get(owner=user)
    assert sorted(queued) == sorted(str(m.id) for m in playtest.matches.all())
    # The games started on the bridge; the worker follows them.
    assert set(playtest.matches.values_list("status", flat=True)) == {Match.Status.PENDING}
    assert all(playtest.matches.values_list("forge_game_id", flat=True))


@pytest.mark.django_db
def test_a_session_the_broker_cannot_queue_leaves_nothing_behind(client, user, fake_forge, monkeypatch):
    from kombu.exceptions import OperationalError

    from playtest import tasks

    def down(*args, **kwargs):
        raise OperationalError("Error 111 connecting to localhost:6379. Connection refused.")

    monkeypatch.setattr(tasks.run_match_task, "delay", down)
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(3)]
    response = client.post("/playtests/create", _form("duel", decks))
    assert response.status_code == 503
    assert b"The playtest could not be queued" in response.content
    assert not Playtest.objects.exists() and not Match.objects.exists()
    assert {g.status for g in fake_forge.games.values()} == {"aborted"}


# Games per opponent (#59).


@pytest.mark.django_db
def test_each_opponent_plays_n_games_with_unique_deterministic_seeds(client, user, fake_forge):
    tested, *opponents = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(3)]
    assert client.post("/playtests/create", _form("duel", [tested, *opponents], games="3")).status_code == 302
    playtest = Playtest.objects.get(owner=user)
    assert playtest.games_per_opponent == 3
    matches = list(playtest.matches.order_by("seed"))
    # seed + opponent index × games + game index
    assert [m.seed for m in matches] == [7, 8, 9, 10, 11, 12]
    assert [m.seats.get(position=1).deck_id for m in matches] == [opponents[0].id] * 3 + [opponents[1].id] * 3
    assert all(m.status == Match.Status.FINISHED for m in matches), [m.error for m in matches]
    # Forge checks the decks once per opponent when the session starts; the
    # other games start when the worker picks them up.
    assert [g["seed"] for g in fake_forge.created[:2]] == [7, 10]
    assert sorted(g["seed"] for g in fake_forge.created) == [7, 8, 9, 10, 11, 12]


@pytest.mark.django_db
def test_one_game_per_opponent_is_the_default(client, user, fake_forge):
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(3)]
    form = _form("duel", decks)
    assert "games" not in form
    client.post("/playtests/create", form)
    playtest = Playtest.objects.get(owner=user)
    assert playtest.games_per_opponent == 1
    assert sorted(playtest.matches.values_list("seed", flat=True)) == [7, 8]


@pytest.mark.django_db
def test_only_the_first_game_of_each_opponent_starts_with_the_session(client, user, fake_forge, monkeypatch):
    from playtest import tasks

    queued = []
    monkeypatch.setattr(tasks.run_match_task, "delay", queued.append)
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(3)]
    assert client.post("/playtests/create", _form("duel", decks, games="2")).status_code == 302
    playtest = Playtest.objects.get(owner=user)
    by_seed = {m.seed: m for m in playtest.matches.all()}
    assert [bool(by_seed[seed].forge_game_id) for seed in (7, 8, 9, 10)] == [True, False, True, False]
    # Every game is queued, game by game across the opponents so none waits behind another's.
    assert queued == [str(by_seed[seed].id) for seed in (7, 9, 8, 10)]


@pytest.mark.django_db
def test_a_refused_deck_is_named_once_however_many_games(client, user, fake_forge):
    tested = make_deck(user, name="Deck 0", fmt="duel")
    odd = make_deck(user, name="Odd", fmt="duel")
    odd.cards[2]["data"]["name"] = "Nope Card"
    odd.save()
    fake_forge.unknown = {"Nope Card"}
    response = client.post("/playtests/create", _form("duel", [tested, odd], games="5"))
    assert response.status_code == 422
    assert response.content.decode().count("Odd: Forge does not know Nope Card.") == 1
    assert len(fake_forge.created) == 0 and not Match.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("games, message", [
    ("0", "Games per opponent must be between 1 and 20."),
    ("21", "Games per opponent must be between 1 and 20."),
    ("two", "Games per opponent must be between 1 and 20."),
])
def test_games_per_opponent_out_of_range_is_refused(client, user, games, message):
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    response = client.post("/playtests/create", _form("duel", decks, games=games))
    assert response.status_code == 422 and message in response.content.decode()
    assert not Playtest.objects.exists()


@pytest.mark.django_db
def test_a_session_has_at_most_fifty_games(client, user):
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(4)]
    response = client.post("/playtests/create", _form("duel", decks, games="17"))
    assert response.status_code == 422
    assert "A playtest plays at most 50 games; 3 opponents × 17 games is 51." in response.content.decode()
    assert not Playtest.objects.exists()


@pytest.mark.django_db
def test_the_form_keeps_the_games_per_opponent(client, user):
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    page = client.get(f"/playtests/new?deck={decks[0].id}").content.decode()
    assert 'name="games"' in page and 'value="1"' in page.split('name="games"', 1)[1].split(">", 1)[0]
    refused = client.post("/playtests/create", _form("duel", decks, games="4", seed="x")).content.decode()
    assert 'value="4"' in refused.split('name="games"', 1)[1].split(">", 1)[0]
    assert "Start 4 games" in refused


def _game(user, playtest, opponent, seed, **fields):
    match = make_match(user, playtest=playtest, seats=("Deck 0", opponent), seed=seed, **fields)
    if fields.get("status") == Match.Status.FINISHED:
        match.winner = match.seats.get(position=0 if fields.pop("won", True) else 1)
        match.save()
    return match


@pytest.mark.django_db
def test_the_session_page_shows_each_matchup_record(client, user):
    from playtest.views import session_view

    playtest = make_playtest(user, seed=10, games_per_opponent=3)
    won = dict(status=Match.Status.FINISHED, end_reason="AllOpponentsLost")
    for seed in (10, 11):
        _game(user, playtest, "Goblins", seed, **won)
    _game(user, playtest, "Goblins", 12, status=Match.Status.RUNNING)
    lost = make_match(user, playtest=playtest, seats=("Deck 0", "Elves"), seed=13, status=Match.Status.FINISHED)
    lost.winner = lost.seats.get(position=1)
    lost.save()
    make_match(user, playtest=playtest, seats=("Deck 0", "Elves"), seed=14, status=Match.Status.FINISHED)
    make_match(user, playtest=playtest, seats=("Deck 0", "Elves"), seed=15, status=Match.Status.FAILED)

    session = session_view(Playtest.objects.get(pk=playtest.pk))
    goblins, elves = session["matchups"]
    assert goblins["opponent"].deck_name == "Goblins" and elves["opponent"].deck_name == "Elves"
    assert (goblins["record"].score, goblins["record"].win_rate, goblins["running"]) == ("2–0–0", 100, 1)
    assert [row["game"] for row in goblins["rows"]] == [1, 2, 3]
    assert (elves["record"].score, elves["record"].win_rate, elves["failed"]) == ("0–1–1", 0, 1)

    page = client.get(f"/playtests/{playtest.id}").content.decode()
    assert "3 games per opponent" in page
    assert "2–0–0" in page and "0–1–1" in page
    assert page.count('href="/playtests/%s/matches/' % playtest.id) == 6


@pytest.mark.django_db
def test_stopping_a_session_stops_every_game_of_every_matchup(client, user, fake_forge, monkeypatch):
    from playtest import tasks

    monkeypatch.setattr(tasks.run_match_task, "delay", lambda match_id: None)
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(3)]
    client.post("/playtests/create", _form("duel", decks, games="3"))
    playtest = Playtest.objects.get(owner=user)
    assert client.post(f"/playtests/{playtest.id}/stop").status_code == 302
    assert set(playtest.matches.values_list("status", flat=True)) == {Match.Status.STOPPED}
    assert {g.status for g in fake_forge.games.values()} == {"aborted"}


@pytest.mark.django_db
def test_run_again_keeps_the_games_per_opponent(client, user, fake_forge):
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    client.post("/playtests/create", _form("duel", decks, games="2"))
    playtest = Playtest.objects.get(owner=user)
    page = client.get(f"/playtests/{playtest.id}").content.decode()
    assert "games=2" in re.search(r'href="(/playtests/new\?[^"]+)"', page).group(1)


@pytest.mark.django_db
def test_the_playtest_list_counts_opponents_not_games(client, user, fake_forge):
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(3)]
    client.post("/playtests/create", _form("duel", decks, games="2"))
    page = client.get("/playtests").content.decode()
    assert "vs <span translate=\"no\">Deck 1, Deck 2</span>" in page


@pytest.mark.django_db
def test_the_home_running_panel_lists_matchups_when_a_session_plays_several_games(client, user):
    playtest = make_playtest(user, seed=1, games_per_opponent=3)
    _game(user, playtest, "Goblins", 1, status=Match.Status.FINISHED)
    _game(user, playtest, "Goblins", 2, status=Match.Status.RUNNING)
    _game(user, playtest, "Goblins", 3)
    page = client.get("/").content.decode()
    panel = page.split('id="running-now"', 1)[1].split("</section>", 1)[0]
    assert panel.count("Goblins") == 1
    assert "1–0–0" in panel and "1 of 3 games done" in panel


@pytest.mark.django_db
def test_the_playtest_page_and_the_match_summary_show_flagged_decisions(client, user):
    playtest = make_playtest(user, fmt="modern")
    match = make_match(user, playtest=playtest, status="finished", summary={
        "turns": 3, "starting_life": 20,
        "seats": [{"seat": 0, "flags": {"bad_attack": 2}}, {"seat": 1, "flags": {}}],
    })
    page = client.get(f"/playtests/{playtest.id}").content.decode()
    assert "Flagged decisions" in page and "2 attack into a killer" in page
    summary = client.get(f"/playtests/{playtest.id}/matches/{match.id}").content.decode()
    assert "2 attack into a killer" in summary and "none" in summary


@pytest.mark.django_db
def test_a_playtest_can_let_forges_ai_play_every_seat(client, user, fake_forge, monkeypatch):
    """Forge AI seats need no key: the bridge plays them itself and no worker is asked."""
    monkeypatch.delenv("OPENROUTER_API_KEY")
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    response = client.post("/playtests/create", _form("duel", decks, agent="forge_ai"))
    assert response.status_code == 302, response.content
    playtest = Playtest.objects.get(owner=user)
    assert playtest.agent == "forge_ai"
    match = playtest.matches.get()
    assert [seat.agent for seat in match.seats.order_by("position")] == ["forge_ai", "forge_ai"]
    assert [s["controller"] for s in fake_forge.created[0]["seats"]] == ["forge_ai", "forge_ai"]
    assert match.status == Match.Status.FINISHED, match.error
    assert not match.events.filter(kind="decision").exists()
    page = client.get(f"/playtests/{playtest.id}").content.decode()
    assert "Forge AI on every seat" in page
    assert f"agent=forge_ai" in page  # Run again keeps the choice


@pytest.mark.django_db
def test_jev_plays_unless_the_form_says_otherwise(client, user, fake_forge):
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    client.post("/playtests/create", _form("duel", decks))
    playtest = Playtest.objects.get(owner=user)
    assert playtest.agent == "jev"
    assert [seat.agent for seat in playtest.matches.get().seats.all()] == ["jev", "jev"]
    response = client.post("/playtests/create", _form("duel", decks, agent="chess_engine"))
    assert response.status_code == 422 and b"Pick who plays" in response.content


@pytest.mark.django_db
def test_the_new_playtest_form_offers_the_player_and_keeps_the_choice(client, user):
    make_deck(user, fmt="duel")
    content = client.get("/playtests/new").content.decode()
    assert 'name="agent" value="jev" checked' in content
    assert 'name="agent" value="forge_ai"' in content
    content = client.get("/playtests/new?agent=forge_ai").content.decode()
    assert 'name="agent" value="forge_ai" checked' in content
