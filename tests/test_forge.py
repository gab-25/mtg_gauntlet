"""Tests for the Forge bridge client (no network: a fake HTTP session)."""

import pytest
import requests

from playtest import forge

from .factories import constructed_cards, stored_cards


class Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class Session:
    """Records requests and answers each from ``replies`` (a path -> Response map)."""

    def __init__(self, replies=None, error=None):
        self.replies = replies or {}
        self.error = error
        self.sent = []

    def request(self, method, url, timeout, **kwargs):
        self.sent.append({"method": method, "url": url, "timeout": timeout, **kwargs})
        if self.error:
            raise self.error
        path = url.split("8080", 1)[1]
        return self.replies[(method, path)]


def _client(replies=None, error=None):
    session = Session(replies, error)
    return forge.ForgeClient("http://bridge:8080/", session=session), session


def test_the_url_comes_from_forge_url(monkeypatch):
    monkeypatch.setenv("FORGE_URL", "http://forge:8080/")
    assert forge.base_url() == "http://forge:8080"
    monkeypatch.delenv("FORGE_URL")
    assert forge.base_url() == forge.DEFAULT_URL


def test_a_commander_deck_becomes_a_decklist_with_its_commander_header():
    text = forge.decklist_text(stored_cards(), has_commander=True)
    assert text.startswith("Commander\n1 Bear Lord\n\nDeck\n40 Forest\n1 Bear 0\n")


def test_a_constructed_deck_keeps_its_sideboard_under_a_header():
    text = forge.decklist_text(constructed_cards(), has_commander=False)
    main, side = text.split("\n\nSideboard\n")
    assert main.startswith("Deck\n4 Spell 0") and "24 Mountain" in main
    assert side.splitlines() == [f"3 Side {i}" for i in range(5)]


def test_formats_map_to_the_bridges_names():
    assert forge.bridge_format("duel") == "duel_commander"
    assert forge.bridge_format("modern") == "modern"
    with pytest.raises(forge.ForgeError, match="two players"):
        forge.bridge_format("commander")


def test_create_game_posts_the_seats_and_returns_the_game():
    game = {"id": "g1", "status": "running", "seats": [{"name": "A", "unsupported_cards": []}]}
    client, session = _client({("POST", "/games"): Response(201, game)})
    seats = [{"name": "A", "decklist": "4 Lightning Bolt"}]
    assert client.create_game("modern", 7, seats) == game
    assert session.sent[0]["url"] == "http://bridge:8080/games"
    assert session.sent[0]["json"] == {"format": "modern", "seed": 7, "seats": seats}


def test_unknown_cards_raise_a_deck_error_naming_them():
    detail = {"detail": {"unresolved": {"A": ["Nope Card"]}}}
    client, _ = _client({("POST", "/games"): Response(422, detail)})
    with pytest.raises(forge.ForgeDeckError) as error:
        client.create_game("modern", 1, [])
    assert error.value.unresolved == {"A": ["Nope Card"]}
    assert "A: Nope Card" in str(error.value)


def test_every_failure_is_a_forge_error():
    client, _ = _client(error=requests.ConnectionError("refused"))
    with pytest.raises(forge.ForgeError, match="unreachable"):
        client.game("g1")
    client, _ = _client({("GET", "/games/g1"): Response(500, text="boom")})
    with pytest.raises(forge.ForgeError, match="HTTP 500"):
        client.game("g1")
    client, _ = _client({("DELETE", "/games/g1"): Response(404)})
    client.abort("g1")  # an already-gone game has nothing to abort


def test_check_deck_needs_a_bridge_that_supports_it():
    client, _ = _client({("POST", "/decks/check"): Response(404)})
    with pytest.raises(forge.ForgeError, match="update mtg_forge_engine"):
        client.check_deck("modern", "4 Lightning Bolt")


@pytest.mark.parametrize("path, call", [
    ("/games/g1", lambda client: client.game("g1")),
    ("/games/g1/events", lambda client: client.events("g1", 3)),
])
def test_a_game_the_bridge_does_not_know_is_its_own_error(path, call):
    client, _ = _client({("GET", path): Response(404, {"detail": "no such game"})})
    with pytest.raises(forge.ForgeGameNotFound, match="no longer knows this game"):
        call(client)


def test_health_reads_a_bridge_still_loading_its_cards():
    client, session = _client({("GET", "/health"): Response(503, text="loading")})
    assert client.health(timeout=2) == {"status": "loading"}
    assert session.sent[0]["timeout"] == 2
