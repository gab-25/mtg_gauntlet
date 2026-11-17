"""HTTP client for the Forge bridge (mtg_forge_engine), the rules engine.

Forge plays the game: it knows the rules and the cards, and its own AI plays
every seat. This module only moves JSON. ``FORGE_URL``
points at the bridge; tests replace :func:`client` with a fake
(``tests/fakeforge.py``), so the suite never needs the container.
"""

import os

import requests

from decks.formats import FORMATS
from decks.rules.legality import board_of

DEFAULT_URL = "http://localhost:8080"
REQUEST_TIMEOUT = 15
# Game statuses on the bridge after which nothing more happens.
ENDED = ("finished", "failed", "aborted")


class ForgeError(Exception):
    """The bridge could not be reached or refused a request."""


class ForgeDeckError(ForgeError):
    """Forge does not know some cards: ``unresolved`` maps a seat name to them."""

    def __init__(self, unresolved: dict[str, list[str]]):
        self.unresolved = unresolved
        named = "; ".join(f"{seat}: {', '.join(cards)}" for seat, cards in unresolved.items())
        super().__init__(f"Forge does not know these cards — {named}")


class ForgeGameNotFound(ForgeError):
    """The bridge does not know the game: it restarted (it keeps no state) or never had it."""

    def __init__(self):
        super().__init__("The Forge bridge no longer knows this game (it restarted?).")


def base_url() -> str:
    return (os.environ.get("FORGE_URL") or DEFAULT_URL).rstrip("/")


def decklist_text(cards: list, *, has_commander: bool) -> str:
    """A stored deck (``Deck.cards``) as the text decklist the bridge reads."""
    sections = {"commander": [], "main": [], "side": []}
    for item in cards:
        line = f"{item['quantity']} {item['data']['name']}"
        if has_commander and item.get("is_commander"):
            sections["commander"].append(line)
        else:
            sections["side" if board_of(item) == "side" else "main"].append(line)
    parts = []
    if sections["commander"]:
        parts.append("Commander\n" + "\n".join(sections["commander"]))
    parts.append("Deck\n" + "\n".join(sections["main"]))
    if sections["side"]:
        parts.append("Sideboard\n" + "\n".join(sections["side"]))
    return "\n\n".join(parts) + "\n"


def bridge_format(fmt: str) -> str:
    forge_format = FORMATS[fmt].forge_format
    if forge_format is None:
        raise ForgeError(f"Forge cannot play {FORMATS[fmt].label} yet: the bridge seats two players.")
    return forge_format


class ForgeClient:
    def __init__(self, url: str | None = None, *, session=None):
        self.url = (url or base_url()).rstrip("/")
        self.http = session or requests

    def _call(self, method: str, path: str, *, timeout: float = REQUEST_TIMEOUT, **kwargs):
        try:
            response = self.http.request(method, f"{self.url}{path}", timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            raise ForgeError(f"Forge bridge unreachable at {self.url}: {exc}") from exc
        return response

    def _json(self, response, what: str):
        if not 200 <= response.status_code < 300:
            raise ForgeError(f"{what}: Forge bridge returned HTTP {response.status_code}: {response.text[:300]}")
        try:
            return response.json()
        except ValueError as exc:
            raise ForgeError(f"{what}: unreadable answer from the Forge bridge ({exc})") from exc

    def health(self, timeout: float = REQUEST_TIMEOUT) -> dict:
        """``{"status": "ready", "cards": N}``, or ``{"status": "loading"}`` while Forge reads its cards."""
        response = self._call("GET", "/health", timeout=timeout)
        if response.status_code == 503:
            return {"status": "loading"}
        return self._json(response, "health")

    def create_game(self, fmt: str, seed: int, seats: list[dict]) -> dict:
        """``seats``: ``[{"name", "decklist"}]``. Returns the bridge's game,
        with each seat's ``unsupported_cards``. Unknown cards raise :class:`ForgeDeckError`."""
        response = self._call("POST", "/games", json={"format": bridge_format(fmt), "seed": seed, "seats": seats})
        if response.status_code == 422:
            detail = _detail(response)
            if isinstance(detail, dict) and "unresolved" in detail:
                raise ForgeDeckError(detail["unresolved"])
        return self._json(response, "create game")

    def check_deck(self, fmt: str, decklist: str) -> dict:
        """``{"unresolved", "unsupported_cards", "missing_commander"}`` for one decklist; no game starts.

        A format the bridge cannot seat is checked as its family: Forge knows
        the same cards, and a commander deck is read the same way.
        """
        checked = FORMATS[fmt].forge_format or ("duel_commander" if FORMATS[fmt].has_commander else "vintage")
        response = self._call("POST", "/decks/check", json={"format": checked, "decklist": decklist})
        if response.status_code == 404:
            raise ForgeError("This Forge bridge cannot check decks: update mtg_forge_engine.")
        return self._json(response, "check deck")

    def game(self, game_id: str) -> dict:
        response = self._call("GET", f"/games/{game_id}")
        if response.status_code == 404:
            raise ForgeGameNotFound()
        return self._json(response, "game")

    def events(self, game_id: str, since: int = 0) -> dict:
        """``{"events": [...], "next": n}``: the game's log from event ``since`` on."""
        response = self._call("GET", f"/games/{game_id}/events", params={"since": since})
        if response.status_code == 404:
            raise ForgeGameNotFound()
        return self._json(response, "events")

    def abort(self, game_id: str) -> None:
        response = self._call("DELETE", f"/games/{game_id}")
        if response.status_code not in (204, 404):
            raise ForgeError(f"abort: Forge bridge returned HTTP {response.status_code}")


def _detail(response):
    try:
        return response.json().get("detail")
    except (ValueError, AttributeError):
        return None


def client() -> ForgeClient:
    """The bridge client every caller uses; tests monkeypatch it with a fake."""
    return ForgeClient()
