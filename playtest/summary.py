"""The match summary: numbers computed from the stored events, never written by a model.

:func:`summarize_match` is pure and deterministic — the same events give the
same dict, every run. It reads Forge's game log (``Turn 3 (Red)``, ``Red cast
Lightning Bolt …``, ``Life: Green 20 > 17``, ``Red played Mountain (31)``) by
the names the seats played under.
"""

import re

from decks.formats import FORMATS

# Forge's log categories left out of the match page and the Markdown export
# (still stored): every step of every turn, and each mana ability.
QUIET_LOG = {"PHASE", "MANA"}

LIFE_CHANGE = re.compile(r" (-?\d+) > (-?\d+)$")
TURN_LINE = re.compile(r"^Turn (\d+) \((.+)\)$")


def table_names(seats: list) -> list[str]:
    """The name each seat plays under: its deck's, plus the seat when a deck sits twice."""
    names = [seat.deck_name for seat in seats]
    return [
        f"{name} (seat {seat.position + 1})" if names.count(name) > 1 else name
        for name, seat in zip(names, seats)
    ]


def event_row(event):
    """Accepts stored ``MatchEvent`` rows and bridge event dicts alike."""
    if isinstance(event, dict):
        return event.get("turn") or 0, event["kind"], event.get("seat"), event.get("text") or "", event.get("payload") or {}
    return event.turn, event.kind, event.seat_position, event.text, event.payload or {}


def _by_prefix(text: str, names: list[tuple[str, int]], prefix: str = "") -> tuple[int, str] | None:
    """``(seat, rest)`` when ``text`` is ``prefix + name + " " + rest``; longest names first."""
    for name, seat in names:
        head = f"{prefix}{name} "
        if text.startswith(head):
            return seat, text[len(head):]
    return None


def names_by_length(seats: list) -> list[tuple[str, int]]:
    """``(table name, position)`` of each seat, longest names first (see :func:`_by_prefix`)."""
    return sorted(((name, seat.position) for name, seat in zip(table_names(seats), seats)), key=lambda x: -len(x[0]))


def life_change(text: str, names: list[tuple[str, int]]) -> tuple[int, int] | None:
    """``(seat, new life)`` of a ``Life: Green 20 > 17`` log line."""
    found = _by_prefix(text, names, "Life: ")
    change = LIFE_CHANGE.search(text)
    return (found[0], int(change.group(2))) if found and change else None


def current_lives(events, seats: list) -> dict[int, int]:
    """Each seat's life after ``events``, for the seats whose life changed in them."""
    names = names_by_length(seats)
    lives = {}
    for _, kind, _, text, payload in map(event_row, events):
        if kind == "log" and payload.get("type") == "LIFE" and (change := life_change(text, names)):
            lives[change[0]] = change[1]
    return lives


def summarize_match(events, seats: list, fmt: str, game: dict) -> dict:
    """The JSON-safe summary of a game.

    ``events`` are the match's events in order, ``seats`` its ``Seat`` rows,
    ``fmt`` the format key and ``game`` the bridge's final game
    (``status``, ``winner``, ``end_reason``, ``turn``).
    """
    rows = [event_row(e) for e in events]
    names = table_names(seats)
    by_length = names_by_length(seats)
    starting = FORMATS[fmt].starting_life
    winner = game.get("winner")
    turns = max([game.get("turn") or 0] + [turn for turn, *_ in rows])

    stats = {
        seat.position: {
            "seat": seat.position,
            "name": name,
            "result": "draw" if winner is None else ("won" if winner == seat.position else "lost"),
            "final_life": starting,
            "life_by_turn": [],
            "turns_taken": 0,
            "spells_cast": 0,
            "lands_played": 0,
            "unsupported_cards": len((seat.coverage or {}).get("unsupported_cards") or []),
        }
        for seat, name in zip(seats, names)
    }
    life = {position: starting for position in stats}
    current_turn = 0

    def close_turns(upto: int) -> None:
        for seat in stats.values():
            while len(seat["life_by_turn"]) < upto:
                seat["life_by_turn"].append(life[seat["seat"]])

    for turn, kind, _, text, payload in rows:
        if kind != "log":
            continue
        if turn > current_turn:
            close_turns(turn - 1)
            current_turn = turn
        category = payload.get("type")
        if category == "LIFE":
            change = life_change(text, by_length)
            if change:
                life[change[0]] = change[1]
        elif category == "TURN":
            line = TURN_LINE.match(text)
            active = next((s for n, s in by_length if line and n == line.group(2)), None)
            if active is not None:
                stats[active]["turns_taken"] += 1
        elif category == "STACK_ADD":
            found = _by_prefix(text, by_length)
            if found and found[1].startswith("cast "):
                stats[found[0]]["spells_cast"] += 1
        elif category == "LAND":
            found = _by_prefix(text, by_length)
            if found and found[1].startswith("played "):
                stats[found[0]]["lands_played"] += 1
    close_turns(turns)

    for seat in stats.values():
        seat["final_life"] = life[seat["seat"]]

    return {
        "winner_seat": winner,
        "end_reason": game.get("end_reason") or "",
        "turns": turns,
        "starting_life": starting,
        "seats": [stats[seat.position] for seat in seats],
    }
