"""Synthetic decks and cards shared by the tests (no network, no Scryfall)."""


def _cost(cmc):
    """A green mana cost of mana value ``cmc``: 4 -> ``{3}{G}``."""
    cmc = int(cmc)
    if not cmc:
        return ""
    return (f"{{{cmc - 1}}}" if cmc > 1 else "") + "{G}"


def scryfall_card(
    name, type_line, cmc=0.0, power=None, toughness=None, card_id=None, image_urls=()
):
    """A processed Scryfall card, as a Deck stores it in ``cards``."""
    return {
        "id": card_id or name,
        "name": name,
        "type_line": type_line,
        "cmc": cmc,
        "power": power,
        "toughness": toughness,
        "image_urls": list(image_urls),
        "color_identity": ["G"],
        "legalities": {"commander": "legal", "duel": "legal"},
        "faces": [{"name": name, "type_line": type_line, "rules_text": "", "mana_cost": _cost(cmc)}],
    }


def stored_cards(commander="Bear Lord"):
    """A legal 100-card deck in the stored ``Deck.cards`` shape."""
    cards = [
        {
            "quantity": 1,
            "is_commander": True,
            "data": scryfall_card(commander, "Legendary Creature — Bear", 4.0, "5", "5"),
        },
        {"quantity": 40, "is_commander": False, "data": scryfall_card("Forest", "Basic Land — Forest")},
    ]
    cards += [
        {
            "quantity": 1,
            "is_commander": False,
            "data": scryfall_card(f"Bear {i}", "Creature — Bear", 2.0, "2", "2"),
        }
        for i in range(59)
    ]
    return cards


def make_deck(owner, name="Bears", fmt="commander", **fields):
    from decks.models import Deck

    defaults = {
        "raw_decklist": "",
        "cards": stored_cards(),
        "commander": "Bear Lord",
        "color_identity": ["G"],
        "status": Deck.Status.READY,
    }
    defaults.update(fields)
    return Deck.objects.create(owner=owner, name=name, format=fmt, **defaults)


def legal_decklist(commander="Atraxa, Praetors' Voice"):
    """A 100-card singleton Commander decklist the creation form accepts."""
    lines = ["Commander", f"1 {commander}", "", "Deck"]
    lines += [f"1 Spell {i}" for i in range(60)]
    lines.append("39 Forest")
    return "\n".join(lines)


def legal_constructed_decklist():
    """A legal 60+15 Modern decklist."""
    lines = [f"4 Spell {i}" for i in range(9)] + ["24 Mountain", "", "Sideboard"]
    lines += [f"3 Side {i}" for i in range(5)]
    return "\n".join(lines)


def constructed_cards(fmt="modern"):
    """A legal 60+15 deck in the stored ``Deck.cards`` shape."""
    cards = [
        {
            "quantity": 4,
            "is_commander": False,
            "board": "main",
            "data": scryfall_card(f"Spell {i}", "Creature — Goblin", 1.0, "2", "1"),
        }
        for i in range(9)
    ]
    cards.append(
        {"quantity": 24, "is_commander": False, "board": "main", "data": scryfall_card("Mountain", "Basic Land — Mountain")}
    )
    cards += [
        {"quantity": 3, "is_commander": False, "board": "side", "data": scryfall_card(f"Side {i}", "Instant", 1.0)}
        for i in range(5)
    ]
    for item in cards:
        item["data"]["legalities"][fmt] = "legal"
        item["data"]["color_identity"] = ["R"]
    return cards


def make_constructed_deck(owner, name="Goblins", fmt="modern"):
    return make_deck(owner, name=name, fmt=fmt, cards=constructed_cards(fmt), commander="", color_identity=["R"])


def make_playtest(owner, deck=None, fmt="duel", deck_name="Deck 0", seed=1, **fields):
    from playtest.models import Playtest

    return Playtest.objects.create(owner=owner, deck=deck, format=fmt, deck_name=deck_name, seed=seed, **fields)


def make_match(owner, playtest=None, seats=("Deck 0", "Deck 1"), **fields):
    """A match of ``playtest`` (a new one if None) with two seats named ``seats``."""
    from playtest.models import Match, Seat

    playtest = playtest or make_playtest(owner, fmt=fields.get("format", "duel"), deck_name=seats[0])
    fields.setdefault("format", playtest.format)
    fields.setdefault("seed", playtest.seed)
    match = Match.objects.create(owner=owner, playtest=playtest, **fields)
    for position, name in enumerate(seats):
        Seat.objects.create(match=match, position=position, deck_name=name)
    return match
