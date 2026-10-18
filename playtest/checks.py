"""Deterministic checks that flag suspicious decisions (#73): no model, no database.

Each check reads the decision traces of a match, in order, and yields
``(trace, reason)`` for the decisions it finds suspicious. Only decisions the
agent took itself are checked (a delegated one is Forge's AI's). The checks
read the bridge's option labels ("Pass priority", "Play …", "Cast …",
"Done (no more attackers)", "Keep this hand…") and Forge's facts
("lethal if unblocked", "dies if blocked by: …"): they never compute a rule.

A flag means "look at this", not "this was wrong": the bridge says which
blockers kill an attacker, not whether they die too, so a bad attack and an
even trade look alike.
"""

import re

# A seven-card hand kept with at most ``low`` or at least ``high`` lands is
# flagged; formats not listed use DEFAULT_MULLIGAN_LANDS.
DEFAULT_MULLIGAN_LANDS = (1, 6)
MULLIGAN_LANDS: dict[str, tuple[int, int]] = {}

DIES_IF_BLOCKED = "dies if blocked by: "
LETHAL = "lethal if unblocked"
BLOCK_PROMPT = re.compile(r"unblocked attackers deal (\d+), your life (-?\d+)")
# "Cast Remand #106 for {1}{U}: …": the card's name.
CAST = re.compile(r"^Cast (.+?) #\d+")
# The flash keyword on a line of its own in a card's text.
FLASH = re.compile(r"(?m)^Flash\b")

# How each check reads on the page.
FLAG_LABELS = {
    "no_effect": "play with no effect",
    "pass_with_plays": "pass with plays left",
    "land_not_played": "land not played",
    "ignored_lethal": "lethal ignored",
    "bad_attack": "attack into a killer",
    "mulligan_keep": "risky keep",
    "worse_outcome": "dominated outcome",
}
# The "do nothing" option of a declaration: one creature at a time ("Done…") or a whole plan.
DECLINES = ("No attack", "No block")
# "Bear #3 (3/3): deals 3 damage if unblocked; lethal if unblocked": a plan's facts, per attacker.
PER_ATTACKER = re.compile(r"^(.+? #\d+(?: \(\d+/\d+\))?): (.*)$")


def _label(trace, index) -> str:
    option = next((o for o in trace.options if o.get("index") == index), None)
    return option["label"] if option else ""


def _labels(trace) -> list[str]:
    return [option["label"] for option in trace.options]


def _you(trace) -> dict:
    return (trace.state or {}).get("you") or {}


def _own_empty_stack(trace) -> bool:
    state = trace.state or {}
    return state.get("active_player") == "you" and not state.get("stack")


def _took(trace) -> bool:
    """Whether the agent's pick is what the game did: not delegated, nor lost on the way."""
    return trace.choice is not None and trace.outcome != "delegated"


def _passes(trace) -> bool:
    return trace.kind == "priority" and _label(trace, trace.choice) == "Pass priority"


def _declines(trace) -> bool:
    label = _label(trace, trace.choice)
    return label.startswith("Done") or label in DECLINES


def _chosen(trace) -> dict | None:
    return next((o for o in trace.options if o.get("index") == trace.choice), None)


def _per_attacker(facts: list[str]) -> list[str]:
    """The facts of each attacker of an option, one string each: a plan prefixes them with
    the attacker, a single attack lists them bare."""
    grouped: dict[str, list[str]] = {}
    for fact in facts:
        found = PER_ATTACKER.match(fact)
        key, text = (found.group(1), found.group(2)) if found else ("", fact)
        grouped.setdefault(key, []).append(text)
    return ["; ".join(texts) for texts in grouped.values()]


def no_effect(traces, fmt):
    for trace in traces:
        if trace.outcome == "no_effect":
            yield trace, f"Forge did not carry out {_label(trace, trace.choice)}"


def _instant_speed(trace) -> set[str]:
    """The cards in hand that can be cast at instant speed: instants and cards with flash."""
    return {
        card.get("name")
        for card in _you(trace).get("hand") or []
        if "Instant" in (card.get("types") or "") or FLASH.search(card.get("text") or "")
    }


def pass_with_plays(traces, fmt):
    """Holding instants and flash cards for the opponent's turn is a plan, not a missed play."""
    for trace in traces:
        if _passes(trace) and trace.step == "MAIN2" and _own_empty_stack(trace):
            held = _instant_speed(trace)
            spells = [
                label for label in _labels(trace)
                if label.startswith("Cast ") and not ((cast := CAST.match(label)) and cast.group(1) in held)
            ]
            if spells:
                yield trace, f"passed the second main phase with {len(spells)} spell{'s' if len(spells) != 1 else ''} castable"


def land_not_played(traces, fmt):
    last_pass, played = {}, set()
    for trace in traces:
        key = (trace.seat_position, trace.turn)
        if trace.kind == "priority" and _label(trace, trace.choice).startswith("Play "):
            played.add(key)
        elif (
            _passes(trace)
            and trace.step in ("MAIN1", "MAIN2")
            and _own_empty_stack(trace)
            and not _you(trace).get("lands_played_this_turn")
            and any(label.startswith("Play ") for label in _labels(trace))
        ):
            last_pass[key] = trace
    for key, trace in last_pass.items():
        if key not in played:
            yield trace, "passed with a land to play and none played this turn"


def ignored_lethal(traces, fmt):
    for trace in traces:
        if not _declines(trace):
            continue
        if trace.kind == "attack":
            lethal = [o["label"] for o in trace.options if any(LETHAL in fact for fact in o.get("facts") or [])]
            if lethal:
                yield trace, f"stopped attacking with a lethal attack offered: {lethal[0]}"
        elif trace.kind == "block" and len(trace.options) > 1:
            found = BLOCK_PROMPT.search(trace.prompt or "")
            if found and int(found.group(1)) >= int(found.group(2)):
                yield trace, f"left {found.group(1)} damage unblocked at {found.group(2)} life"


def bad_attack(traces, fmt):
    """Every attacker of the pick dies to a blocker and none is lethal."""
    for trace in traces:
        if trace.kind != "attack":
            continue
        attackers = _per_attacker((_chosen(trace) or {}).get("facts") or [])
        if not attackers or any(LETHAL in facts for facts in attackers):
            continue
        killers = [facts.split(DIES_IF_BLOCKED, 1)[1].split(";")[0] for facts in attackers if DIES_IF_BLOCKED in facts]
        if len(killers) == len(attackers):
            yield trace, f"attacks into blockers that kill it: {', '.join(dict.fromkeys(killers))}"


def _counts(outcome: dict) -> tuple:
    """The numbers of an outcome, each better when higher: your life, the opponents'
    life lost, your creatures kept, the opponents' creatures killed, your cards in hand."""
    you, opponents = outcome["you"], outcome.get("opponents") or []
    return (
        you["life"][1],
        sum(o["life"][0] - o["life"][1] for o in opponents),
        -len(you.get("lost") or []),
        sum(len(o.get("lost") or []) for o in opponents),
        you["hand"][1],
    )


def worse_outcome(traces, fmt):
    """Another option's simulated outcome is at least as good on every count and better on one."""
    for trace in traces:
        chosen = _chosen(trace)
        if not chosen or not chosen.get("outcome"):
            continue
        mine = _counts(chosen["outcome"])
        for other in trace.options:
            if other is chosen or not other.get("outcome"):
                continue
            theirs = _counts(other["outcome"])
            if all(t >= m for t, m in zip(theirs, mine)) and theirs != mine:
                yield trace, f"{other['label']} is better on every count"
                break


def mulligan_keep(traces, fmt):
    low, high = MULLIGAN_LANDS.get(fmt, DEFAULT_MULLIGAN_LANDS)
    for trace in traces:
        if trace.kind != "mulligan" or not _label(trace, trace.choice).startswith("Keep"):
            continue
        hand = _you(trace).get("hand") or []
        if len(hand) != 7:
            continue
        lands = sum("Land" in (card.get("types") or "") for card in hand)
        if lands <= low or lands >= high:
            yield trace, f"kept seven cards with {lands} land{'s' if lands != 1 else ''}"


CHECKS = (no_effect, pass_with_plays, land_not_played, ignored_lethal, bad_attack, mulligan_keep, worse_outcome)


def check_match(traces, fmt: str) -> dict[int, list[dict]]:
    """``{trace id: [{"check", "reason"}, ...]}`` for the flagged decisions, in ``CHECKS`` order."""
    chosen = [trace for trace in traces if _took(trace)]
    flags: dict[int, list[dict]] = {}
    for check in CHECKS:
        for trace, reason in check(chosen, fmt):
            flags.setdefault(trace.id, []).append({"check": check.__name__, "reason": reason})
    return flags


def trace_stats(traces) -> dict[int, dict]:
    """Per seat with traces, merged into the match summary: ``{"flags": {check: count}}``
    and, when the bridge said what Forge's AI would pick (#75), ``agreement``
    (``rated``, ``agree``, and ``by_kind``: ``{kind: [rated, agree]}``)."""
    stats: dict[int, dict] = {}
    for trace in traces:
        seat = stats.setdefault(trace.seat_position, {"flags": {}})
        for flag in trace.flags or []:
            seat["flags"][flag["check"]] = seat["flags"].get(flag["check"], 0) + 1
        if _took(trace) and trace.forge_ai_choice is not None:
            agreement = seat.setdefault("agreement", {"rated": 0, "agree": 0, "by_kind": {}})
            same = trace.choice == trace.forge_ai_choice
            agreement["rated"] += 1
            agreement["agree"] += same
            kind = agreement["by_kind"].setdefault(trace.kind, [0, 0])
            kind[0] += 1
            kind[1] += same
    for seat in stats.values():
        seat["flags"] = dict(sorted(seat["flags"].items()))
        if "agreement" in seat:
            seat["agreement"]["by_kind"] = dict(sorted(seat["agreement"]["by_kind"].items()))
    return stats
