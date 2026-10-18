"""Reading and checking the new-playtest form: a deck to test and its opponents."""

import random
from dataclasses import dataclass

from decks.formats import FORMATS, playable_formats
from decks.models import Deck, MetagameDeck

from .models import Seat

# An opponent picker value for a metagame deck.
METAGAME_PREFIX = "metagame:"
# Every seat of a new playtest is played by Forge's AI: the user picks decks, not
# agents. Jev (Seat.Agent.JEV) is the experimental agent the benchmark measures
# (manage.py benchmark --agent jev); on seed 1000 it won 38% of the games Forge's
# AI won 60% of (2026-10-18, docs/agents.md), so it plays no playtest seat.
SEAT_AGENT = Seat.Agent.FORGE_AI
# The most opponents one session plays: each match is a few hundred Jev calls.
MAX_OPPONENTS = 10
# The most games against one opponent, and in a whole session (opponents ×
# games): the worker follows a bounded number of matches at once, and a long
# queue holds back every other session behind it.
MAX_GAMES_PER_OPPONENT = 20
MAX_GAMES = 50
# The format a submitted form without one falls back to.
DEFAULT_MATCH_FORMAT = next(iter(playable_formats()))


@dataclass
class DeckChoice:
    # A user's deck, or None for a metagame deck.
    deck: Deck | None
    metagame_deck: MetagameDeck | None = None

    @property
    def name(self) -> str:
        if self.deck is not None:
            return self.deck.name
        return self.metagame_deck.archetype


@dataclass
class PlaytestForm:
    format: str
    seed: int
    deck: Deck
    opponents: list[DeckChoice]
    games: int = 1


def _int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _opponent(value: str, decks: dict, fmt: str, errors: list) -> DeckChoice | None:
    label = FORMATS[fmt].label
    if value.startswith(METAGAME_PREFIX):
        meta = MetagameDeck.objects.filter(pk=_int(value.removeprefix(METAGAME_PREFIX))).first()
        if meta is None:
            errors.append("An opponent metagame deck is gone; sync again or pick another.")
        elif meta.format != fmt:
            errors.append(f"Opponent {meta.archetype} is not a {label} metagame deck.")
        elif meta.status != MetagameDeck.Status.READY:
            errors.append(f"Opponent {meta.archetype} could not be imported ({meta.error}).")
        else:
            return DeckChoice(deck=None, metagame_deck=meta)
        return None
    deck = decks.get(value)
    if deck is None:
        errors.append("Pick opponents among your imported decks and the metagame decks.")
        return None
    if deck.format != fmt:
        errors.append(f"Opponent {deck.name} is not a {label} deck.")
        return None
    return DeckChoice(deck=deck)


def parse_playtest_form(post, user) -> tuple[PlaytestForm | None, list[str]]:
    """Returns the chosen session, or ``None`` and every reason it is refused."""
    errors = []
    fmt = post.get("format") if post.get("format") in FORMATS else DEFAULT_MATCH_FORMAT
    rules = FORMATS[fmt]
    if fmt not in playable_formats():
        errors.append(
            f"{rules.label} cannot be played yet: the Forge engine seats two players "
            f"({', '.join(f.label for f in playable_formats().values())})."
        )

    raw_seed = (post.get("seed") or "").strip()
    seed = _int(raw_seed) if raw_seed else random.randrange(2**31)
    if seed is None:
        errors.append("The seed must be a whole number.")

    games = _int(post.get("games") or 1)
    if games is None or not 1 <= games <= MAX_GAMES_PER_OPPONENT:
        errors.append(f"Games per opponent must be between 1 and {MAX_GAMES_PER_OPPONENT}.")
        games = None

    decks = {str(d.id): d for d in Deck.objects.filter(owner=user, status=Deck.Status.READY)}
    deck = decks.get(post.get("deck") or "")
    if deck is None:
        errors.append("Pick the deck to test among your imported decks.")
    elif deck.format != fmt:
        errors.append(f"{deck.name} is not a {rules.label} deck.")

    values = list(dict.fromkeys(v for v in post.getlist("opponents") if v))
    if not values:
        errors.append("Pick at least one opponent.")
    elif len(values) > MAX_OPPONENTS:
        errors.append(f"Pick at most {MAX_OPPONENTS} opponents; {len(values)} chosen.")
    opponents = [o for o in (_opponent(v, decks, fmt, errors) for v in values[:MAX_OPPONENTS]) if o]
    if games and values and len(values) * games > MAX_GAMES:
        errors.append(
            f"A playtest plays at most {MAX_GAMES} games; "
            f"{len(values)} opponent{'s' if len(values) != 1 else ''} × {games} games is {len(values) * games}."
        )

    if errors:
        return None, errors
    return PlaytestForm(format=fmt, seed=seed, deck=deck, opponents=opponents, games=games), []
