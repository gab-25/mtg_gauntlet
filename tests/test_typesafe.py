"""Tests for the Jev client over OpenRouter's System One endpoint (no network)."""

import pytest
import requests

from playtest.agents import typesafe

from .cards import Response

QUESTIONS = {"q": typesafe.choice("Which?", {"a": "first", "b": "second"})}
ANSWER = {"q": {"type": "choice", "choice": "b", "confidence": 0.9, "probabilities": {"a": 0.1, "b": 0.9}}}


def test_requires_an_openrouter_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(typesafe.TypeSafeError, match="OPENROUTER_API_KEY"):
        typesafe.system_one("state", QUESTIONS)


def test_posts_the_native_format_to_openrouter(monkeypatch):
    sent = {}

    def fake_post(url, headers, json, timeout):
        sent.update(url=url, headers=headers, json=json)
        return Response(payload={"model": "typesafe/jev-1.13", "answers": ANSWER, "usage": {"cost": 0.0001}})

    monkeypatch.setattr(requests, "post", fake_post)
    result = typesafe.system_one("the state", QUESTIONS, key="k")
    assert sent["url"] == "https://openrouter.ai/api/v1/systemone"
    assert sent["headers"]["Authorization"] == "Bearer k"
    assert sent["headers"]["X-Title"] == "mtg_gauntlet"
    assert sent["json"] == {"model": "~typesafe/jev-latest", "state": "the state", "questions": QUESTIONS}
    # The versioned model that answered, not the moving alias.
    assert result["model"] == "typesafe/jev-1.13"
    assert result["answers"] == ANSWER


def test_retries_rate_limits_honouring_retry_after(monkeypatch):
    replies = [
        Response(429, text="slow down", headers={"retry-after": "3"}),
        Response(529, text="overloaded"),
        Response(payload={"model": "m", "answers": ANSWER}),
    ]
    monkeypatch.setattr(requests, "post", lambda *a, **k: replies.pop(0))
    waits = []
    result = typesafe.system_one("s", QUESTIONS, key="k", sleep=waits.append)
    assert result["answers"] == ANSWER
    assert waits == [3.0, 2.0]


def test_gives_up_after_bounded_retries(monkeypatch):
    calls = []

    def limited(*args, **kwargs):
        calls.append(1)
        return Response(429, text="slow down")

    monkeypatch.setattr(requests, "post", limited)
    with pytest.raises(typesafe.TypeSafeError, match="HTTP 429"):
        typesafe.system_one("s", QUESTIONS, key="k", sleep=lambda s: None)
    assert len(calls) == typesafe.MAX_ATTEMPTS


@pytest.mark.parametrize(
    "response, message",
    [
        (Response(401, text="bad key"), "HTTP 401"),
        (Response(200, payload={"nope": 1}), "unexpected"),
        (Response(200, payload=None), "unexpected"),
    ],
)
def test_reports_failures(monkeypatch, response, message):
    monkeypatch.setattr(requests, "post", lambda *a, **k: response)
    with pytest.raises(typesafe.TypeSafeError, match=message):
        typesafe.system_one("s", QUESTIONS, key="k")


def test_reports_network_errors(monkeypatch):
    def offline(*args, **kwargs):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(requests, "post", offline)
    with pytest.raises(typesafe.TypeSafeError, match="offline"):
        typesafe.system_one("s", QUESTIONS, key="k")


def test_read_choice_refuses_an_option_that_was_not_offered():
    assert typesafe.read_choice(ANSWER["q"], ["a", "b"]) == ("b", 0.9, {"a": 0.1, "b": 0.9})
    with pytest.raises(typesafe.TypeSafeError):
        typesafe.read_choice(ANSWER["q"], ["a"])
    with pytest.raises(typesafe.TypeSafeError):
        typesafe.read_choice({"choice": "a"}, ["a"])


def test_a_choice_has_at_most_255_options():
    with pytest.raises(ValueError):
        typesafe.choice("?", {str(i): "x" for i in range(256)})


@pytest.mark.parametrize(
    "response, status",
    [
        (Response(200, payload={"data": {"limit": None, "limit_remaining": None, "usage": 3}}), "ready"),
        (Response(200, payload={"data": {"limit": 10, "limit_remaining": 4.5}}), "ready"),
        (Response(200, payload={"data": {"limit": 10, "limit_remaining": 0}}), "no credit"),
        (Response(402, text="pay"), "no credit"),
        (Response(401, text="bad key"), "invalid key"),
        (Response(500, text="down"), "unreachable"),
        (Response(200, payload={"nope": 1}), "unreachable"),
    ],
)
def test_key_status_reads_the_key_endpoint(monkeypatch, response, status):
    calls = []

    def fake_get(url, headers, timeout):
        calls.append((url, headers["Authorization"], timeout))
        return response

    monkeypatch.setattr(requests, "get", fake_get)
    assert typesafe.key_status("k") == status
    assert calls == [(typesafe.KEY_URL, "Bearer k", typesafe.STATUS_TIMEOUT)]


def test_key_status_without_a_key_or_network(monkeypatch):
    def offline(*args, **kwargs):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(requests, "get", offline)
    assert typesafe.key_status("k") == "unreachable"
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert typesafe.key_status() == "no API key"


def test_score_question_shape_and_limits():
    assert typesafe.score("How good?", ["lost", "even", "won"]) == {
        "type": "score", "instructions": "How good?", "criteria": ["lost", "even", "won"]}
    with pytest.raises(ValueError):
        typesafe.score("x", ["only one"])
    with pytest.raises(ValueError):
        typesafe.score("x", [str(n) for n in range(11)])


def test_read_score():
    answer = {"type": "score", "score": 3.7, "probabilities": {"3": 0.3, "4": 0.7}, "confidence": 0.6}
    assert typesafe.read_score(answer) == (3.7, 0.6, {"3": 0.3, "4": 0.7})
    with pytest.raises(typesafe.TypeSafeError):
        typesafe.read_score({"type": "score"})
    with pytest.raises(typesafe.TypeSafeError):
        typesafe.read_score(None)
