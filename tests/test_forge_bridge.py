"""Opt-in: matches against a real Forge bridge.

Run with the bridge up (``docker compose up -d forge``)::

    FORGE_URL=http://localhost:8080 uv run pytest -m forge
"""

import os

import pytest

from playtest import forge, runner
from playtest.models import Match, Seat

from .factories import make_deck, make_playtest, scryfall_card

pytestmark = [
    pytest.mark.forge,
    pytest.mark.django_db,
    pytest.mark.skipif(not os.environ.get("FORGE_URL"), reason="needs a Forge bridge at FORGE_URL"),
]

RED = [("Mountain", 20), ("Goblin Guide", 4), ("Monastery Swiftspear", 4), ("Lightning Bolt", 4), ("Lava Spike", 4),
       ("Rift Bolt", 4), ("Skewer the Critics", 4), ("Searing Blaze", 4), ("Eidolon of the Great Revel", 4),
       ("Light Up the Stage", 4), ("Boros Charm", 4)]
GREEN = [("Forest", 24), ("Llanowar Elves", 4), ("Grizzly Bears", 4), ("Giant Growth", 4), ("Garruk's Companion", 4),
         ("Leatherback Baloth", 4), ("Kalonian Tusker", 4), ("Rancor", 4), ("Strangleroot Geist", 4), ("Elvish Mystic", 4)]


def _deck(user, name, entries):
    cards = [{"quantity": qty, "is_commander": False, "board": "main", "data": scryfall_card(card, "Card")} for card, qty in entries]
    return make_deck(user, name=name, fmt="modern", cards=cards, commander="")


def _match(user, seed):
    match = Match.objects.create(owner=user, playtest=make_playtest(user, fmt="modern", seed=seed), format="modern", seed=seed)
    for position, deck in enumerate((_deck(user, "Red", RED), _deck(user, "Green", GREEN))):
        Seat.objects.create(match=match, position=position, deck=deck, deck_name=deck.name)
    return match


def _play(user, seed):
    match = _match(user, seed)
    runner.run_match(match.id)
    match.refresh_from_db()
    return match


def test_a_forge_ai_duel_plays_on_the_bridge_and_replays_from_its_seed(django_user_model):
    user = django_user_model.objects.create_user(username="u")
    first = _play(user, 21)
    assert first.status == Match.Status.FINISHED, first.error
    assert first.events.filter(kind="game_over").exists()
    assert first.summary["turns"] == first.turn > 0
    second = _play(user, 21)
    # Every event but Forge's wall-clock line ("Match Duration 0m : 02s").
    texts = lambda match: [t for t in match.events.values_list("text", flat=True) if not t.startswith("Match Duration")]  # noqa: E731
    assert texts(first) == texts(second)


def test_a_card_forge_does_not_know_is_named(django_user_model):
    user = django_user_model.objects.create_user(username="u")
    match = _match(user, 1)
    seat = match.seats.get(position=0)
    seat.deck.cards.append({"quantity": 1, "is_commander": False, "board": "main", "data": scryfall_card("Nope Card", "Card")})
    seat.deck.save()
    with pytest.raises(forge.ForgeDeckError) as error:
        runner.start_game(match)
    assert error.value.unresolved == {"Red": ["Nope Card"]}

