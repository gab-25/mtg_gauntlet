"""Numbers the pages show, computed from matches already loaded: no queries here.

Every record is seen from seat 0, the deck under test. Only finished matches
count; a match that failed, was stopped or is still playing has no result.
"""

from dataclasses import dataclass
from datetime import timedelta

from django.urls import reverse

from decks.formats import FORMATS

# A metagame snapshot older than this may no longer match the field.
METAGAME_STALE_AFTER = timedelta(days=7)

RESULTS = ("won", "lost", "draw")


@dataclass
class Record:
    won: int = 0
    lost: int = 0
    draw: int = 0
    # Rounded percentages and averages; None when no match is finished.
    win_rate: int | None = None
    avg_turns: float | None = None

    @property
    def played(self) -> int:
        return self.won + self.lost + self.draw

    @property
    def score(self) -> str:
        return f"{self.won}–{self.lost}–{self.draw}"


def record(matches) -> Record:
    finished = [match for match in matches if match.result in RESULTS]
    result = Record(
        won=sum(match.result == "won" for match in finished),
        lost=sum(match.result == "lost" for match in finished),
        draw=sum(match.result == "draw" for match in finished),
    )
    if not finished:
        return result
    result.win_rate = round(100 * result.won / len(finished))
    turns = [(match.summary or {}).get("turns") or match.turn for match in finished]
    result.avg_turns = round(sum(turns) / len(turns), 1)
    return result


def opponent_source(seat) -> str:
    """Where an opponent comes from: the metagame, the user's decks, or a deck since deleted."""
    if seat.metagame_deck_id and seat.metagame_deck is not None:
        return f"Metagame #{seat.metagame_deck.rank}"
    if seat.deck_id:
        return "Your deck"
    return "Removed deck"


@dataclass
class OpponentRecord:
    name: str
    source: str
    record: Record


def record_by_opponent(matches) -> list[OpponentRecord]:
    """One row per opponent (seat 1), most played first."""
    groups: dict[tuple, list] = {}
    seats: dict[tuple, object] = {}
    for match in matches:
        opponent = next((seat for seat in match.seats.all() if seat.position == 1), None)
        if opponent is None:
            continue
        if opponent.metagame_deck_id:
            key = ("metagame", opponent.metagame_deck_id)
        elif opponent.deck_id:
            key = ("deck", opponent.deck_id)
        else:
            key = ("name", opponent.deck_name)
        groups.setdefault(key, []).append(match)
        seats.setdefault(key, opponent)
    ranked = sorted(groups.items(), key=lambda item: (-len(item[1]), seats[item[0]].deck_name))
    return [
        OpponentRecord(seats[key].deck_name, opponent_source(seats[key]), record(group))
        for key, group in ranked
    ]


@dataclass
class Notice:
    title: str
    text: str
    action: str
    url: str
    # warn | info | muted
    tone: str = "warn"


def needs_attention(decks, metagame_synced: dict, now) -> list[Notice]:
    """What the home page asks the user to look at, decks first."""
    notices = []
    for deck in decks:
        url = reverse("deck_detail", args=[deck.id])
        if deck.is_busy:
            notices.append(Notice(f"{deck.name} is still importing", "Fetching its cards from Scryfall.",
                                  "Open decks", reverse("deck_list"), "info"))
        elif deck.status != deck.Status.READY:
            notices.append(Notice(f"{deck.name} could not be imported", deck.error or "The import failed.",
                                  "Open decks", reverse("deck_list"), "warn"))
        elif not deck.forge_checked:
            notices.append(Notice(f"{deck.name} is not checked",
                                  "Forge has not seen this deck yet: a card it does not know would refuse the playtest.",
                                  "Check with Forge", url, "warn"))
        elif deck.unknown_cards:
            notices.append(Notice(f"{deck.name} has cards Forge does not know", ", ".join(deck.unknown_cards),
                                  "Open deck", url, "warn"))
    for fmt in sorted({deck.format for deck in decks}):
        synced = metagame_synced.get(fmt)
        if synced is not None and now - synced > METAGAME_STALE_AFTER:
            notices.append(Notice(f"{FORMATS[fmt].label} metagame is {(now - synced).days} days old",
                                  "Opponents picked from it may not match the current field.",
                                  "See metagame", f"{reverse('metagame')}?format={fmt}", "muted"))
    return notices
