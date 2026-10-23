"""An in-memory Forge bridge: the same methods as ``playtest.forge.ForgeClient``.

Each game is a small scripted duel — mulligans, lands, Shocks, one attacker
and one blocker a turn — written as a generator that yields every decision an
external seat must answer. Forge-AI seats and delegated decisions are decided
by the game's seeded RNG, so the same seed and the same answers give the same
events, like the real bridge. Events, decisions and fallbacks have the
bridge's shape, and the log lines read like Forge's.
"""

import random
import re
import threading
import uuid

from playtest.forge import ForgeDeckError, ForgeError, ForgeGameNotFound

HEADERS = {"commander", "deck", "sideboard"}
# The Forge method that takes a decision of each kind when it falls back.
AI_METHODS = {"mulligan": "mulliganKeepHand", "priority": "chooseSpellAbilityToPlay",
              "attack": "declareAttackers", "block": "declareBlockers"}
LINE = re.compile(r"^(\d+)\s+(.+)$")


def decklist_names(text: str) -> list[str]:
    names = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.lower() in HEADERS:
            continue
        match = LINE.match(line)
        names.append(match.group(2) if match else line)
    return names


class FakeGame:
    def __init__(self, fake, fmt: str, seed: int, seats: list[dict], max_turns: int, starting_life: int):
        self.fake = fake
        self.id = uuid.uuid4().hex
        # The bridge reports a format by its label.
        self.format = "Duel Commander" if fmt == "duel_commander" else fmt.capitalize()
        self.seed = seed
        self.names = [seat["name"] for seat in seats]
        self.controllers = [seat.get("controller", "external") for seat in seats]
        self.rng = random.Random(seed)
        self.max_turns = max_turns
        self.life = [starting_life, starting_life]
        self.turn = 0
        self.phase = ""
        self.status = "running"
        self.winner = None
        self.end_reason = None
        self.events: list[dict] = []
        self.pending: dict | None = None
        self.leased = False
        self.next_card = 1
        self.script = self._play()
        # A game between Forge's AIs runs on the bridge's thread and takes a few seconds;
        # here it plays out on the first read of its events or state, so a game created
        # and aborted at once is "aborted", as on the bridge.
        self.started = False
        if "external" in self.controllers:
            self._start()

    def _start(self) -> None:
        if not self.started:
            self.started = True
            self._advance(None)

    # --- the bridge's view of the game ------------------------------------------

    def summary(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "format": self.format,
            "seed": self.seed,
            "turn": self.turn,
            "winner": self.winner,
            "end_reason": self.end_reason,
            "error": None,
        }

    def view(self, seat: int) -> dict:
        other = 1 - seat
        return {
            "format": self.format,
            "turn": self.turn,
            "active_seat": None if self.turn == 0 else (self.turn - 1) % 2,
            "phase": self.phase,
            "you": {
                "seat": seat, "name": self.names[seat], "life": self.life[seat],
                "hand": [{"id": 900, "name": "Shock", "mana_cost": "{R}", "types": "Instant", "text": "Shock deals 2 damage to any target."}],
                "library": 40, "graveyard": [], "exile": [], "mana_pool": 0, "mana_available": 1,
                "lands_played": 0, "commanders": [],
            },
            "opponents": [{
                "seat": other, "name": self.names[other], "life": self.life[other], "hand": 5, "library": 40,
                "graveyard": [], "exile": [], "commanders": [],
            }],
            "battlefield": {
                "0": [{"id": 1, "name": "Bear", "types": "Creature - Bear", "text": "", "power": 2, "toughness": 2,
                       "tapped": False, "summoning_sick": False, "damage": 0, "counters": {}, "attached_to": None}],
                "1": [],
            },
            "stack": [],
            "combat": None,
        }

    # --- the script ------------------------------------------------------------

    def _event(self, kind: str, seat, text: str, payload: dict | None = None) -> None:
        self.events.append({
            "seq": len(self.events), "turn": self.turn, "phase": "" if kind == "log" else self.phase,
            "seat": seat, "kind": kind, "text": text, "payload": payload or {},
        })

    def _log(self, category: str, text: str) -> None:
        self._event("log", None, text, {"type": category})

    def _card(self) -> int:
        self.next_card += 1
        return self.next_card

    def _ask(self, seat: int, kind: str, prompt: str, options: list[tuple[str, list[str]]]):
        """Yields a decision for an external seat; returns the chosen index."""
        if self.controllers[seat] == "forge_ai":
            return self.rng.randrange(len(options))
        decision = {
            "decision_id": uuid.uuid4().hex,
            "game_id": self.id,
            "seat": seat,
            "kind": kind,
            "prompt": prompt,
            "view": self.view(seat),
            "options": [{"index": i, "label": label, "facts": facts} for i, (label, facts) in enumerate(options)],
        }
        if self.fake.forge_ai_choice is not None:
            decision["forge_ai_choice"] = self.fake.forge_ai_choice(decision)
        answer = yield decision
        if answer["choice"] is None:
            self._event("fallback", seat, f"{kind}: decided by Forge AI (delegate)", {"kind": kind, "reason": "delegate", **answer["meta"]})
            # Like the bridge, Forge's AI then logs the decision it takes.
            method = AI_METHODS[kind]
            self._event("forge_ai", seat, f"{method}: decided by Forge AI", {"method": method})
            return self.rng.randrange(len(options))
        choice = answer["choice"]
        self._event("decision", seat, f"{kind}: {options[choice][0]}", {"kind": kind, "choice": choice, **answer["meta"]})
        return choice

    def _damage(self, target: int, amount: int) -> None:
        before = self.life[target]
        self.life[target] -= amount
        self._log("LIFE", f"Life: {self.names[target]} {before} > {self.life[target]}")
        if self.life[target] <= 0 and self.winner is None:
            self.winner = 1 - target
            self.end_reason = "AllOpponentsLost"

    def _play(self):
        for seat in (0, 1):
            choice = yield from self._ask(seat, "mulligan", "Mulligan? Your hand: Shock, Mountain", [("Keep this hand", []), ("Mulligan", [])])
            self._log("MULLIGAN", f"{self.names[seat]} has {'kept' if choice == 0 else 'mulliganed'} a hand of 7 cards")
        while self.winner is None:
            self.turn += 1
            if self.turn > self.max_turns:
                self.turn = self.max_turns
                self.end_reason = "turn_limit"
                break
            active, other = (self.turn - 1) % 2, self.turn % 2
            self._log("TURN", f"Turn {self.turn} ({self.names[active]})")
            self.phase = "MAIN1"
            for _ in range(3):
                land, shock = self._card(), self._card()
                options = [
                    ("Pass priority", []),
                    (f"Play Mountain #{land}", []),
                    (f"Cast Shock #{shock} for {{R}}: deals 2 damage to any target", []),
                ]
                if self.fake.failing_cast:
                    options.append((f"Cast Stifle #{self._card()} for {{U}}: Counter target activated or triggered ability.", []))
                choice = yield from self._ask(active, "priority", f"Turn {self.turn}, MAIN1: you have priority", options)
                while choice == 3:
                    # Like the bridge (#71): Forge finds no target on the stack, the
                    # cast aborts and the very same decision comes back.
                    choice = yield from self._ask(active, "priority", f"Turn {self.turn}, MAIN1: you have priority", options)
                if choice == 0:
                    break
                if choice == 1:
                    self._log("LAND", f"{self.names[active]} played Mountain ({land})")
                else:
                    self._event("log", None, f"Mountain ({land}) - {{T}}: Add {{R}}.", {"type": "MANA"})
                    self._log("STACK_ADD", f"{self.names[active]} cast Shock targeting [{self.names[other]}]")
                    self._damage(other, 2)
                    if self.winner is not None:
                        return
            self.phase = "COMBAT_DECLARE_ATTACKERS"
            attack = yield from self._ask(active, "attack", "Declare attackers", [
                ("Done (no more attackers)", []),
                (f"Attack with Bear #{self._card()} (3/3) -> {self.names[other]}", ["deals 3 damage if unblocked"]),
            ])
            if attack:
                self.phase = "COMBAT_DECLARE_BLOCKERS"
                block = yield from self._ask(other, "block", f"Declare blockers (unblocked attackers deal 3, your life {self.life[other]})", [
                    ("Done (no more blockers)", []),
                    ("Block Bear with Wall", ["your blocker survives"]),
                ])
                self.phase = "COMBAT_DAMAGE"
                for seat in (active, other):
                    if self.controllers[seat] == "external":
                        self._event("forge_ai", seat, "assignCombatDamage: decided by Forge AI", {"method": "assignCombatDamage"})
                if not block:
                    self._damage(other, 3)
            self.phase = "CLEANUP"

    def _advance(self, answer) -> None:
        try:
            self.pending = next(self.script) if answer is None else self.script.send(answer)
            self.leased = False
        except StopIteration:
            self.pending = None
            self._end("finished")

    def _end(self, status: str) -> None:
        self.status = status
        self.phase = "CLEANUP" if status == "finished" else self.phase
        self._log("GAME_OUTCOME", f"{self.names[self.winner]} has won because all opponents have lost" if self.winner is not None else "The game is a draw")
        self._event("game_over", None, f"Game over: {self.end_reason}, winner seat {self.winner}",
                    {"winner": self.winner, "end_reason": self.end_reason})

    def answer(self, choice, delegate, meta) -> None:
        self._advance({"choice": None if delegate else choice, "meta": meta})

    def abort(self) -> None:
        if self.status != "running":
            return
        self.started = True  # a game aborted before anyone read it never plays
        self.pending = None
        self.end_reason = "aborted"
        self.winner = None
        self._end("aborted")


class FakeForge:
    """A bridge in memory. ``unknown`` cards are refused (422); ``unsupported``
    maps a card name to Forge's "unsupported" flag; ``failing_cast`` adds a
    priority option whose cast Forge never carries out."""

    def __init__(self, *, unknown=(), unsupported=(), max_turns=12, starting_life=20, failing_cast=False,
                 forge_ai_choice=None):
        self.unknown = set(unknown)
        # A function of the decision giving the option Forge's AI would pick (#75);
        # None leaves it out of the lease, like today's bridge.
        self.forge_ai_choice = forge_ai_choice
        # Offer a "Cast Stifle" that never happens, the same decision coming back.
        self.failing_cast = failing_cast
        self.unsupported = set(unsupported)
        self.max_turns = max_turns
        self.starting_life = starting_life
        self.games: dict[str, FakeGame] = {}
        self.created: list[dict] = []
        self.answers: list[dict] = []
        self.cond = threading.Condition()

    def _game(self, game_id: str) -> FakeGame:
        game = self.games.get(game_id)
        if game is None:
            raise ForgeGameNotFound()
        return game

    def health(self, timeout: float = 0) -> dict:
        return {"status": "ready", "cards": 1}

    def create_game(self, fmt: str, seed: int, seats: list[dict]) -> dict:
        from playtest.forge import bridge_format

        unresolved = {}
        for seat in seats:
            missing = [name for name in decklist_names(seat["decklist"]) if name in self.unknown]
            if missing:
                unresolved[seat["name"]] = missing
        if unresolved:
            raise ForgeDeckError(unresolved)
        with self.cond:
            game = FakeGame(self, bridge_format(fmt), seed, seats, self.max_turns, self.starting_life)
            self.games[game.id] = game
            self.created.append({"format": bridge_format(fmt), "seed": seed, "seats": seats})
            self.cond.notify_all()
        return {
            **game.summary(),
            "seats": [
                {"name": seat["name"], "controller": seat.get("controller", "external"),
                 "unsupported_cards": [n for n in decklist_names(seat["decklist"]) if n in self.unsupported]}
                for seat in seats
            ],
        }

    def check_deck(self, fmt: str, decklist: str) -> dict:
        names = decklist_names(decklist)
        return {
            "unresolved": [n for n in names if n in self.unknown],
            "unsupported_cards": [n for n in names if n in self.unsupported],
            "missing_commander": False,
        }

    def game(self, game_id: str) -> dict:
        with self.cond:
            game = self._game(game_id)
            game._start()
            return game.summary()

    def events(self, game_id: str, since: int = 0) -> dict:
        with self.cond:
            game = self._game(game_id)
            game._start()
            events = list(game.events[since:])
        return {"events": events, "next": since + len(events)}

    def lease(self, wait: float = 30, game_id: str | None = None) -> dict | None:
        with self.cond:
            def ready():
                for game in self.games.values():
                    if game.pending and not game.leased and game_id in (None, game.id):
                        return game
                return None

            game = ready()
            if game is None:
                self.cond.wait_for(lambda: ready() is not None, timeout=wait)
                game = ready()
            if game is None:
                return None
            game.leased = True
            return game.pending

    def answer(self, decision_id: str, choice=None, *, delegate: bool = False, meta: dict | None = None) -> bool:
        with self.cond:
            game = next((g for g in self.games.values() if g.pending and g.pending["decision_id"] == decision_id), None)
            if game is None:
                return False
            if not delegate and not 0 <= choice < len(game.pending["options"]):
                raise ForgeError(f"choice {choice} out of range")
            self.answers.append({"decision_id": decision_id, "choice": choice, "delegate": delegate, "meta": meta or {}})
            game.answer(choice, delegate, meta or {})
            self.cond.notify_all()
        return True

    def abort(self, game_id: str) -> None:
        with self.cond:
            game = self.games.get(game_id)
            if game is not None:
                game.abort()
            self.cond.notify_all()


def sample_decision(kind: str = "attack", options=None) -> dict:
    """A bridge decision for the agent tests: Red (seat 0) attacking Green."""
    game = FakeGame(FakeForge(), "modern", 1, [{"name": "Red", "decklist": ""}, {"name": "Green", "decklist": ""}], 12, 20)
    game.turn, game.phase = 3, "COMBAT_DECLARE_ATTACKERS"
    view = game.view(0)
    view["you"]["graveyard"] = [{"id": 70, "name": "Lightning Bolt", "mana_cost": "{R}", "types": "Instant", "text": "Lightning Bolt deals 3 damage to any target."}]
    view["battlefield"]["1"] = [{"id": 5, "name": "Wall", "types": "Creature - Wall", "text": "Defender", "power": 0, "toughness": 4,
                                 "tapped": False, "summoning_sick": True, "damage": 0, "counters": {"P1P1": 1}, "attached_to": None},
                                {"id": 6, "name": "Rancor", "types": "Enchantment - Aura", "text": "Enchant creature",
                                 "tapped": False, "summoning_sick": False, "damage": 0, "counters": {}, "attached_to": 5}]
    options = options or [
        ("Done (no more attackers)", []),
        ("Attack with Bear #3 (3/3) -> Green", ["deals 3 damage if unblocked"]),
        ("Attack with Goblin #4 (1/1) -> Green", ["dies if blocked by: Wall"]),
    ]
    return {
        "decision_id": "d1",
        "game_id": game.id,
        "seat": 0,
        "kind": kind,
        "prompt": "Declare attackers",
        "view": view,
        "options": [{"index": i, "label": label, "facts": facts} for i, (label, facts) in enumerate(options)],
    }
