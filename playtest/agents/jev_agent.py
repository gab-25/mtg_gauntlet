"""An agent that asks Jev which of Forge's options to take.

One Jev request per decision: the ``state`` is the bridge's view filtered to
what a decision needs, and the single ``choice`` question's options *are* the
bridge's options — keyed by their index, described by their label plus the
facts Forge's AI helpers computed for them ("lethal if unblocked", "dies if
blocked by: …"), because Jev does not do arithmetic. The options are shuffled
with the agent's seeded RNG before every request and mapped back by key: Jev
leans toward the first option, and Forge's first option is always "Pass" or
"Done".

The answer's confidence and distribution travel with the decision to the
bridge's event. Below ``MIN_CONFIDENCE``, or when Jev cannot be reached,
the agent delegates: Forge's AI decides, and the fallback is logged with the
reason.
"""

import json
import logging
import random
import time
from dataclasses import replace

from . import typesafe
from .base import Decision, delegate

logger = logging.getLogger(__name__)

# Confidence under which Forge's AI decides instead. Jev's confidence measures how peaked its distribution is,
# not the chosen option's probability. Measured on Forge's decisions (a Modern
# Burn seat against Forge's AI, 23 decisions, jev-1.13, 2026-10-13): median
# 0.38, quartiles 0.20 / 0.82; 0.2 hands over 17% of decisions (the near-ties
# between two burn spells), 0.4 would hand over half. The Python kernel's
# decisions measured the same (222, median ~0.4).
MIN_CONFIDENCE = 0.2
# How many of Jev's top options a decision keeps for the log.
TOP_PROBABILITIES = 3

INSTRUCTIONS = (
    "You play {you} in a game of Magic: The Gathering ({format}), played under the full rules by the "
    "Forge engine. It is turn {turn}, {active} turn, {phase}. Your goal is to win: reduce your opponent "
    "to 0 life while staying alive. The state lists life totals, your hand with card text, every "
    "battlefield and the stack. Mana is paid automatically and combat damage is assigned for you. "
    "Each option is legal, with facts already computed for it. {kind_line}{step_line}{prompt_line} "
    "Forge asks: \"{prompt}\". Which option is best right now?"
)

# One line per decision kind: what picking an option means.
KIND_LINES = {
    "mulligan": "Keep the opening hand, or mulligan to a new one with one card fewer.",
    "bottom": "After a mulligan, pick a card to put on the bottom of your library.",
    "priority": "You have priority: play a land, cast a spell, activate an ability, or pass.",
    "target": "Pick a target for the spell or ability you are putting on the stack.",
    "attack": "Declare attackers one at a time; \"Done\" ends the declaration.",
    "block": "Declare blockers one at a time; \"Done\" ends the declaration.",
    "choose_cards": "Pick cards one at a time; \"Done\" ends the selection once enough are picked.",
    "confirm": "Answer yes or no.",
    "mode": "Pick a mode of the spell or ability.",
}


# What passing priority means where the game stands (#79): Jev sees one
# decision at a time and cannot tell that a pass in the second main phase ends
# the turn, or that mana spent in the upkeep is gone for the main phase.
STACK_LINE = "The stack is not empty: passing lets the top of the stack resolve; respond only if it is worth answering."
OPPONENT_TURN_LINE = (
    "It is your opponent's turn: only instants, cards with flash and abilities can be used now; "
    "use them to answer what your opponent does, otherwise pass."
)
OWN_STEP_LINES = {
    "UPKEEP": "Your turn has just begun: passing moves on to your first main phase, where you can play a land and "
              "cast any spell; spend mana now only for what must happen now.",
    "MAIN1": "Your first main phase: play a land if you have not yet, and cast what should be on the battlefield before combat.",
    "COMBAT": "Combat on your turn: act only to change this combat.",
    "MAIN2": "Your second main phase, the last chance this turn for a land and sorcery-speed spells: passing with an "
             "empty stack ends your turn and unused mana is lost. Keep mana open only for instant-speed cards you hold.",
    "END": "Your turn is ending.",
}
OWN_STEP_LINES["DRAW"] = OWN_STEP_LINES["UPKEEP"]
OWN_STEP_LINES["END_OF_TURN"] = OWN_STEP_LINES["CLEANUP"] = OWN_STEP_LINES.pop("END")

# What an answer to a prompt means, when Forge's rules text does not say it plainly.
COMMAND_ZONE_LINE = (
    "Yes moves your commander to the command zone, where you can cast it again; "
    "No leaves it in the graveyard or exile for the rest of the game."
)


def step_line(view: dict, kind: str) -> str:
    if kind != "priority":
        return ""
    if view.get("stack"):
        return " " + STACK_LINE
    if view.get("active_seat") != view["you"]["seat"]:
        return " " + OPPONENT_TURN_LINE
    phase = view.get("phase") or ""
    line = OWN_STEP_LINES.get("COMBAT" if phase.startswith("COMBAT") else phase, "")
    return " " + line if line else ""


def prompt_line(kind: str, prompt: str) -> str:
    if kind == "confirm" and "command zone" in prompt:
        return " " + COMMAND_ZONE_LINE
    return ""


# -- the state Jev sees ----------------------------------------------------------


def _card(card: dict) -> dict:
    keep = {k: card[k] for k in ("name", "mana_cost", "types", "text", "power", "toughness") if card.get(k) not in (None, "")}
    if "tax" in card:
        keep["commander_tax"] = card["tax"]
    return keep


def _permanent(perm: dict, names: dict[int, str]) -> dict:
    keep = {k: perm[k] for k in ("name", "types", "text", "power", "toughness") if perm.get(k) not in (None, "")}
    for flag in ("tapped", "summoning_sick"):
        if perm.get(flag):
            keep[flag] = True
    if perm.get("damage"):
        keep["damage"] = perm["damage"]
    if perm.get("counters"):
        keep["counters"] = perm["counters"]
    if perm.get("attached_to") is not None:
        keep["attached_to"] = names.get(perm["attached_to"], "a permanent")
    return keep


def _zone(cards: list, full: bool):
    return [_card(c) for c in cards] if full else len(cards)


def jev_state(view: dict, *, kind: str = "", prompt: str = "") -> str:
    """The bridge's view filtered to what a decision needs: no ids, no history.

    Graveyards and exile are counts, unless the decision is about them.
    """
    about_graveyard = kind in ("choose_cards", "target") and any(
        word in prompt.lower() for word in ("graveyard", "exile")
    )
    you = view["you"]
    seats = {you["seat"]: "you"} | {o["seat"]: o["name"] for o in view.get("opponents", [])}
    names = {p["id"]: p["name"] for perms in view.get("battlefield", {}).values() for p in perms}
    state = {
        "turn": view.get("turn"),
        "phase": view.get("phase"),
        "active_player": seats.get(view.get("active_seat"), ""),
        "you": {
            "life": you["life"],
            "hand": [_card(c) for c in you.get("hand", [])],
            "library": you.get("library"),
            "graveyard": _zone(you.get("graveyard", []), about_graveyard),
            "exile": _zone(you.get("exile", []), about_graveyard),
            "mana_available": you.get("mana_available"),
            "lands_played_this_turn": you.get("lands_played"),
            "commanders": [_card(c) for c in you.get("commanders", [])],
            "battlefield": [_permanent(p, names) for p in view.get("battlefield", {}).get(str(you["seat"]), [])],
        },
        "opponents": [
            {
                "name": o["name"],
                "life": o["life"],
                "cards_in_hand": o.get("hand"),
                "library": o.get("library"),
                "graveyard": _zone(o.get("graveyard", []), about_graveyard),
                "exile": _zone(o.get("exile", []), about_graveyard),
                "commanders": [_card(c) for c in o.get("commanders", [])],
                "battlefield": [_permanent(p, names) for p in view.get("battlefield", {}).get(str(o["seat"]), [])],
            }
            for o in view.get("opponents", [])
        ],
        "stack": [
            {"text": item.get("text"), "source": item.get("source"), "controller": seats.get(item.get("controller"), "")}
            for item in view.get("stack", [])
        ],
    }
    if view.get("combat"):
        state["combat"] = [
            {"attacker": a["name"], "attacking": a.get("defender"), "blocked_by": [names.get(b, "a creature") for b in a.get("blocked_by", [])]}
            for a in view["combat"].get("attackers", [])
        ]
    if not state["you"]["commanders"]:
        del state["you"]["commanders"]
        for o in state["opponents"]:
            if not o["commanders"]:
                del o["commanders"]
    return json.dumps(state, separators=(",", ":"), ensure_ascii=False)


def describe_option(option: dict) -> str:
    facts = option.get("facts") or []
    return option["label"] + (" — " + "; ".join(facts) if facts else "")


def instructions(view: dict, kind: str, prompt: str) -> str:
    you = view["you"]
    active = view.get("active_seat")
    if active is None:
        whose = "before the first"
    elif active == you["seat"]:
        whose = "your"
    else:
        whose = "your opponent's"
    return INSTRUCTIONS.format(
        you=you.get("name") or f"seat {you['seat'] + 1}",
        format=view.get("format", ""),
        turn=view.get("turn", 0),
        active=whose,
        phase=(view.get("phase") or "game start").replace("_", " ").lower(),
        kind_line=KIND_LINES.get(kind, ""),
        step_line=step_line(view, kind),
        prompt_line=prompt_line(kind, prompt),
        prompt=prompt,
    )


# -- the agent -------------------------------------------------------------------


class JevAgent:
    """Plays through Jev; never blocks the game: when unsure or unreachable it delegates."""

    # Model-backed: the worker pool meters its calls and the match's wall clock.
    metered = True

    def __init__(self, model: str | None = None, *, seed: int | None = None, ask=None, min_confidence: float | None = None):
        self.model = model or typesafe.MODEL
        self.rng = random.Random(f"{seed}:jev")
        self.ask = ask or typesafe.system_one
        self.min_confidence = MIN_CONFIDENCE if min_confidence is None else min_confidence

    def choose(self, view: dict, options: list[dict], *, kind: str = "", prompt: str = "") -> Decision:
        if len(options) > typesafe.MAX_CHOICE_OPTIONS:
            return delegate("error", f"{len(options)} options: Jev takes at most {typesafe.MAX_CHOICE_OPTIONS}")
        keys = [f"o{option['index']}" for option in options]
        by_key = dict(zip(keys, options))
        shuffled = list(keys)
        self.rng.shuffle(shuffled)
        criteria = {key: describe_option(by_key[key]) for key in shuffled}
        text = instructions(view, kind, prompt)
        state = jev_state(view, kind=kind, prompt=prompt)
        question = typesafe.choice(text, criteria)
        # What Jev is shown, kept for the decision trace (#72).
        trace = {
            "instructions": text,
            "state": json.loads(state),
            "options": [
                {"index": by_key[key]["index"], "label": by_key[key]["label"], "facts": list(by_key[key].get("facts") or [])}
                for key in shuffled
            ],
            "probabilities": {},
            "latency_ms": None,
        }
        started = time.monotonic()
        try:
            response = self.ask(state, {"action": question}, model=self.model)
            picked, confidence, probabilities = typesafe.read_choice(response["answers"].get("action"), by_key)
        except (typesafe.TypeSafeError, KeyError, TypeError, AttributeError) as exc:
            logger.warning("Jev agent (%s) delegated to Forge's AI: %s", self.model, exc)
            trace["latency_ms"] = round(1000 * (time.monotonic() - started))
            return delegate("error", f"Jev unavailable: {exc}", model=self.model, trace=trace)
        trace["latency_ms"] = round(1000 * (time.monotonic() - started))
        trace["probabilities"] = {
            str(by_key[key]["index"]): round(p, 4) for key, p in probabilities.items() if key in by_key
        }
        top = tuple(
            (by_key[key]["label"], p)
            for key, p in sorted(probabilities.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_PROBABILITIES]
            if key in by_key
        )
        return replace(self.gate(by_key[picked]["index"], confidence, top, response.get("model", self.model)), trace=trace)

    def gate(self, choice: int, confidence: float, top, model: str) -> Decision:
        """The confidence gate: the one place an uncertain decision is handed over (#44)."""
        if confidence < self.min_confidence:
            return delegate(
                "low_confidence",
                f"low confidence {confidence:.2f} (below {self.min_confidence:.2f})",
                confidence=confidence,
                probabilities=top,
                model=model,
            )
        return Decision(choice, confidence=confidence, probabilities=top, model=model)
