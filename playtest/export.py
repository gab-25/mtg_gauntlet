"""A match as a file for an offline review (#76): JSON lines or a Markdown narration.

Everything here only reads the database and returns text; the management
commands write it to a file or stdout. Nothing is sent anywhere.
"""

import json

from django.utils.text import slugify

from .summary import QUIET_LOG


def _events(match, seat):
    """The match's events in order; ``seat`` keeps that seat's events and the table-wide ones."""
    for event in match.events.all():
        if seat is not None and event.seat_position not in (seat, None):
            continue
        yield event


def match_jsonl(match, *, seat=None) -> str:
    """One JSON object per event, in order."""
    lines = [
        json.dumps(
            {
                "seq": event.seq,
                "turn": event.turn,
                "step": event.step,
                "seat": event.seat_position,
                "kind": event.kind,
                "text": event.text,
                "payload": event.payload,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        for event in _events(match, seat)
    ]
    return "".join(f"{line}\n" for line in lines)


def match_markdown(match, *, seat=None) -> str:
    """A readable narration of Forge's log, per turn."""
    seats = list(match.seats.all())
    out = [f"# {' vs '.join(s.deck_name for s in seats)}", "", f"{match.get_format_display()} · seed {match.seed} · {match.status}", ""]
    turn = None
    for event in _events(match, seat):
        if event.kind == "log" and (event.payload or {}).get("type") in QUIET_LOG:
            continue
        if event.turn != turn:
            turn = event.turn
            out += ["", f"## Turn {turn}" if turn else "## Before the game", ""]
        out.append(f"- {event.text}")
    return "\n".join(out) + "\n"


FORMATS = {"jsonl": match_jsonl, "md": match_markdown}


def file_name(match, ext: str) -> str:
    """``<seed>-<opponent>.<ext>``: one file per match of a playtest."""
    opponent = next((s.deck_name for s in match.seats.all() if s.position == 1), "")
    return f"{match.seed}-{slugify(opponent) or 'opponent'}.{ext}"
