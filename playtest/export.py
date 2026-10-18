"""A match as a file for an offline review (#76): JSON lines or a Markdown narration.

Everything here only reads the database and returns text; the management
commands write it to a file or stdout. Nothing is sent anywhere. A model
reading an export reviews choices after the game: it is never the rules engine.
"""

import json

from django.utils.text import slugify

from .checks import FLAG_LABELS
from .summary import QUIET_LOG


def trace_dict(trace) -> dict:
    """The JSON-safe trace of a decision, each option with Jev's probability."""
    return {
        "id": trace.id,
        "kind": trace.kind,
        "prompt": trace.prompt,
        "state": trace.state,
        "options": [
            {**option, "p": trace.probabilities.get(str(option["index"])), "score": trace.scores.get(str(option["index"]))}
            for option in trace.options
        ],
        "choice": trace.choice,
        "confidence": trace.confidence,
        "outcome": trace.outcome,
        "flags": trace.flags,
        "fallback_kind": trace.fallback_kind,
        "fallback_reason": trace.fallback_reason,
        "forge_ai_choice": trace.forge_ai_choice,
        "model": trace.model,
        "latency_ms": trace.latency_ms,
    }


def _rows(match, seat, flagged):
    """``(event, trace or None)`` in order; ``seat`` keeps that seat's events and the
    table-wide ones, ``flagged`` only the flagged decisions."""
    traces = {trace.event_id: trace for trace in match.traces.exclude(event=None)}
    for event in match.events.all():
        trace = traces.get(event.id)
        if seat is not None and event.seat_position not in (seat, None):
            continue
        if flagged and not (trace and trace.flags):
            continue
        yield event, trace


def match_jsonl(match, *, seat=None, flagged=False) -> str:
    """One JSON object per event, in order; a decision carries its ``trace``."""
    lines = []
    for event, trace in _rows(match, seat, flagged):
        line = {
            "seq": event.seq,
            "turn": event.turn,
            "step": event.step,
            "seat": event.seat_position,
            "kind": event.kind,
            "text": event.text,
            "payload": event.payload,
        }
        if trace is not None:
            line["trace"] = trace_dict(trace)
        lines.append(json.dumps(line, ensure_ascii=False, sort_keys=True))
    return "".join(f"{line}\n" for line in lines)


def _permanents(permanents) -> str:
    return ", ".join(p.get("name", "?") + (" (tapped)" if p.get("tapped") else "") for p in permanents or []) or "—"


def _compact_state(state: dict) -> list[str]:
    you = state.get("you") or {}
    hand = ", ".join(card.get("name", "?") for card in you.get("hand") or []) or "—"
    lines = [f"- You: {you.get('life')} life; hand: {hand}; battlefield: {_permanents(you.get('battlefield'))}"]
    for opponent in state.get("opponents") or []:
        lines.append(
            f"- {opponent.get('name')}: {opponent.get('life')} life, {opponent.get('cards_in_hand')} cards in hand;"
            f" battlefield: {_permanents(opponent.get('battlefield'))}"
        )
    if state.get("stack"):
        lines.append("- Stack: " + "; ".join(item.get("text") or "" for item in state["stack"]))
    return lines


def _decision_md(trace) -> list[str]:
    out = [f"**{trace.kind}** — {trace.prompt}", "", *_compact_state(trace.state or {}), ""]
    for option in trace.options:
        p = trace.probabilities.get(str(option["index"]))
        score = trace.scores.get(str(option["index"]))
        mark = "→" if option["index"] == trace.choice else "·"
        facts = f" — {'; '.join(option['facts'])}" if option.get("facts") else ""
        summary = f" — {option['outcome']['summary']}" if option.get("outcome") else ""
        tail = (f" `{p:.2f}`" if p is not None else "") + (f" `score {score:.2f}`" if score is not None else "")
        out.append(f"- {mark} {option['label']}{facts}{summary}{tail}")
    verdict = f"Outcome: {trace.get_outcome_display().lower() if trace.outcome else 'pending'}"
    if trace.choice is None:
        verdict += f" (Forge AI decided: {trace.fallback_kind})"
    if trace.confidence is not None:
        verdict += f", confidence {trace.confidence:.2f}"
    out += ["", verdict]
    out += [f"- Flag: {FLAG_LABELS.get(flag['check'], flag['check'])}: {flag['reason']}" for flag in trace.flags or []]
    return [*out, ""]


def match_markdown(match, *, seat=None, flagged=False) -> str:
    """A readable narration per turn: Forge's log, and each decision with what Jev saw and chose."""
    seats = list(match.seats.all())
    out = [f"# {' vs '.join(s.deck_name for s in seats)}", "", f"{match.get_format_display()} · seed {match.seed} · {match.status}", ""]
    turn = None
    for event, trace in _rows(match, seat, flagged):
        if event.kind == "log" and (event.payload or {}).get("type") in QUIET_LOG:
            continue
        if event.turn != turn:
            turn = event.turn
            out += ["", f"## Turn {turn}" if turn else "## Before the game", ""]
        if trace is not None:
            out += ["", f"### Seat {event.seat_position + 1}, {event.step or 'game start'}", "", *_decision_md(trace)]
        else:
            out.append(f"- {event.text}")
    return "\n".join(out) + "\n"


FORMATS = {"jsonl": match_jsonl, "md": match_markdown}


def file_name(match, ext: str) -> str:
    """``<seed>-<opponent>.<ext>``: one file per match of a playtest."""
    opponent = next((s.deck_name for s in match.seats.all() if s.position == 1), "")
    return f"{match.seed}-{slugify(opponent) or 'opponent'}.{ext}"
