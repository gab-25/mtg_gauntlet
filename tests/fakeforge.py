"""An in-memory Forge bridge: the same methods as ``playtest.forge.ForgeClient``.

Each game is a small scripted duel — mulligans, lands, Shocks, one attacker
and one blocker a turn — played by "Forge's AI": every choice comes from the
game's seeded RNG, so the same seed gives the same events, like the real
bridge. The game moves one turn each time its events are read, so a test can
stop it halfway. Events have the bridge's shape, and the log lines read like
Forge's.
"""

import random
import re
import threading
import uuid

from playtest.forge import ForgeDeckError, ForgeGameNotFound

HEADERS = {"commander", "deck", "sideboard"}
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
        self.rng = random.Random(seed)
        self.max_turns = max_turns
        self.life = [starting_life, starting_life]
        self.turn = 0
        self.phase = ""
        self.status = "running"
        self.winner = None
        self.end_reason = None
        self.events: list[dict] = []
        self.next_card = 1
        self.script = self._play()

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

    def _choose(self, options: list) -> int:
        """Forge's AI picks one of ``options``: the game's seeded RNG."""
        return self.rng.randrange(len(options))

    def _damage(self, target: int, amount: int) -> None:
        before = self.life[target]
        self.life[target] -= amount
        self._log("LIFE", f"Life: {self.names[target]} {before} > {self.life[target]}")
        if self.life[target] <= 0 and self.winner is None:
            self.winner = 1 - target
            self.end_reason = "AllOpponentsLost"

    def _play(self):
        """The game, one turn per step: it yields after the mulligans and after each turn."""
        for seat in (0, 1):
            kept = self._choose(["keep", "mulligan"]) == 0
            self._log("MULLIGAN", f"{self.names[seat]} has {'kept' if kept else 'mulliganed'} a hand of 7 cards")
        yield
        while self.winner is None:
            self.turn += 1
            if self.turn > self.max_turns:
                self.turn = self.max_turns
                self.end_reason = "turn_limit"
                return
            active, other = (self.turn - 1) % 2, self.turn % 2
            self._log("TURN", f"Turn {self.turn} ({self.names[active]})")
            self.phase = "MAIN1"
            for _ in range(3):
                land, shock = self._card(), self._card()
                choice = self._choose(["pass", "land", "shock"])
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
            if self._choose(["no attack", "attack"]):
                self.phase = "COMBAT_DAMAGE"
                if not self._choose(["no block", "block"]):
                    self._damage(other, 3)
            self.phase = "CLEANUP"
            yield

    def step(self) -> None:
        """Plays the game's next turn; the last one ends it."""
        if self.status != "running":
            return
        try:
            next(self.script)
        except StopIteration:
            self._end("finished")

    def _end(self, status: str) -> None:
        self.status = status
        self.phase = "CLEANUP" if status == "finished" else self.phase
        self._log("GAME_OUTCOME", f"{self.names[self.winner]} has won because all opponents have lost" if self.winner is not None else "The game is a draw")
        self._event("game_over", None, f"Game over: {self.end_reason}, winner seat {self.winner}",
                    {"winner": self.winner, "end_reason": self.end_reason})

    def abort(self) -> None:
        if self.status != "running":
            return
        self.end_reason = "aborted"
        self.winner = None
        self._end("aborted")


class FakeForge:
    """A bridge in memory. ``unknown`` cards are refused (422); ``unsupported``
    maps a card name to Forge's "unsupported" flag."""

    def __init__(self, *, unknown=(), unsupported=(), max_turns=12, starting_life=20):
        self.unknown = set(unknown)
        self.unsupported = set(unsupported)
        self.max_turns = max_turns
        self.starting_life = starting_life
        self.games: dict[str, FakeGame] = {}
        self.created: list[dict] = []
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
                {"name": seat["name"],
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
            return self._game(game_id).summary()

    def events(self, game_id: str, since: int = 0) -> dict:
        with self.cond:
            game = self._game(game_id)
            game.step()
            events = list(game.events[since:])
        return {"events": events, "next": since + len(events)}

    def abort(self, game_id: str) -> None:
        with self.cond:
            game = self.games.get(game_id)
            if game is not None:
                game.abort()
            self.cond.notify_all()

