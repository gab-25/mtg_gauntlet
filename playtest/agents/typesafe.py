"""Jev client: TypeSafe AI's System One model, through OpenRouter.

Jev takes a ``state`` and a map of typed questions and returns typed answers
(for a ``choice``: the picked option, a probability per option and a
confidence), never free text. OpenRouter serves it at ``/api/v1/systemone`` in
TypeSafe's native format, keyed by ``OPENROUTER_API_KEY``; OpenRouter only
adds ``id``, ``provider`` and ``usage.cost`` to the response.

No SDK: one endpoint does not justify a dependency, and plain ``requests.post``
keeps the client testable with a monkeypatch.
"""

import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

API_URL = "https://openrouter.ai/api/v1/systemone"
# The key's own state (validity, credit left): asking it spends no credit.
KEY_URL = "https://openrouter.ai/api/v1/key"

# Every Jev seat uses this alias, which follows Jev releases; the response
# names the exact version.
MODEL = "~typesafe/jev-latest"

# A match runs in a background thread: never wait forever on one request.
REQUEST_TIMEOUT = 30
# The sidebar asks the key's state: it waits for it less.
STATUS_TIMEOUT = 5

# Rate limited (429) or overloaded (529): retried with exponential backoff.
RETRY_STATUSES = {429, 529}
MAX_ATTEMPTS = 4
BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 20.0

# Jev's documented limit on the options of one choice question.
MAX_CHOICE_OPTIONS = 255

# Optional attribution headers, used by OpenRouter to credit the calling app.
APP_URL = "https://github.com/gab-25/mtg_gauntlet"
APP_TITLE = "mtg_gauntlet"


class TypeSafeError(Exception):
    """Jev could not be reached or gave no usable answer."""


def api_key() -> str | None:
    return os.environ.get("OPENROUTER_API_KEY") or None


def key_status(key: str | None = None) -> str:
    """The OpenRouter key's state, from ``KEY_URL``: "ready", "no credit",
    "invalid key", "unreachable", or "no API key" (then nothing is asked).

    It says whether the key works, not whether Jev itself answers: only a
    paid call to the model would tell.
    """
    key = key or api_key()
    if not key:
        return "no API key"
    try:
        response = requests.get(KEY_URL, headers={"Authorization": f"Bearer {key}"}, timeout=STATUS_TIMEOUT)
    except requests.RequestException:
        return "unreachable"
    if response.status_code in (401, 403):
        return "invalid key"
    if response.status_code == 402:
        return "no credit"
    if not 200 <= response.status_code < 300:
        return "unreachable"
    try:
        remaining = response.json()["data"]["limit_remaining"]
    except (ValueError, LookupError, TypeError):
        return "unreachable"
    return "no credit" if remaining is not None and remaining <= 0 else "ready"


def choice(instructions, criteria: dict) -> dict:
    """A ``choice`` question; ``criteria`` maps each option key to its description."""
    if not criteria:
        raise ValueError("a choice needs at least one option")
    if len(criteria) > MAX_CHOICE_OPTIONS:
        raise ValueError(f"a choice takes at most {MAX_CHOICE_OPTIONS} options")
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


# Jev's documented range for the levels of a score question.
MIN_SCORE_LEVELS, MAX_SCORE_LEVELS = 2, 10


def score(instructions, levels: list) -> dict:
    """A ``score`` question: ``levels`` are the ordered descriptions, lowest first; the
    answer's ``score`` is the probability-weighted level number (0 to ``len(levels) - 1``)."""
    if not MIN_SCORE_LEVELS <= len(levels) <= MAX_SCORE_LEVELS:
        raise ValueError(f"a score takes {MIN_SCORE_LEVELS} to {MAX_SCORE_LEVELS} levels, not {len(levels)}")
    return {"type": "score", "instructions": instructions, "criteria": list(levels)}


def read_score(answer) -> tuple[float, float, dict]:
    """``(score, confidence, probabilities by level number)`` of one score answer.

    Raises :class:`TypeSafeError` when the answer is malformed.
    """
    try:
        value = float(answer["score"])
        confidence = float(answer["confidence"])
        probabilities = {str(k): float(v) for k, v in (answer.get("probabilities") or {}).items()}
    except (LookupError, TypeError, ValueError, AttributeError) as exc:
        raise TypeSafeError(f"malformed score answer ({exc})") from exc
    return value, confidence, probabilities


def _retry_after(response) -> float | None:
    value = (getattr(response, "headers", None) or {}).get("retry-after")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def system_one(
    state,
    questions: dict,
    *,
    model: str | None = None,
    key: str | None = None,
    sleep=time.sleep,
) -> dict:
    """Asks ``questions`` about ``state``; returns ``{"model", "answers", "usage"}``.

    ``model`` in the result is the versioned id that answered (the alias moves).
    Retries on 429/529, honouring ``retry-after``; ``sleep`` is injectable so
    tests don't wait. Raises :class:`TypeSafeError` on a missing key, a network
    failure, a non-2xx answer, or a response without answers.
    """
    key = key or api_key()
    if not key:
        raise TypeSafeError("no OpenRouter API key configured (set OPENROUTER_API_KEY)")
    model = model or MODEL

    response = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = requests.post(
                API_URL,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": APP_URL,
                    "X-Title": APP_TITLE,
                },
                json={"model": model, "state": state, "questions": questions},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise TypeSafeError(f"Jev request failed: {exc}") from exc
        if response.status_code not in RETRY_STATUSES or attempt == MAX_ATTEMPTS - 1:
            break
        wait = _retry_after(response)
        if wait is None:
            wait = BACKOFF_SECONDS * 2**attempt
        logger.info("Jev returned HTTP %s, retrying in %.1fs", response.status_code, wait)
        sleep(min(wait, MAX_BACKOFF_SECONDS))

    if not 200 <= response.status_code < 300:
        raise TypeSafeError(f"Jev returned HTTP {response.status_code}: {response.text[:300]}")
    try:
        data = response.json()
        answers = data["answers"]
    except (ValueError, LookupError, TypeError) as exc:
        raise TypeSafeError(f"unexpected Jev response ({exc})") from exc
    if not isinstance(answers, dict):
        raise TypeSafeError("unexpected Jev response (answers is not an object)")
    return {"model": str(data.get("model") or model), "answers": answers, "usage": data.get("usage") or {}}


def read_choice(answer, options) -> tuple[str, float, dict]:
    """Checks one choice answer against the options asked: ``(choice, confidence, probabilities)``.

    Raises :class:`TypeSafeError` when the answer is malformed or names an
    option that was not offered.
    """
    try:
        picked = answer["choice"]
        confidence = float(answer["confidence"])
        probabilities = {str(k): float(v) for k, v in (answer.get("probabilities") or {}).items()}
    except (LookupError, TypeError, ValueError, AttributeError) as exc:
        raise TypeSafeError(f"malformed choice answer ({exc})") from exc
    if picked not in options:
        raise TypeSafeError(f"Jev answered {picked!r}, which was not an option")
    return picked, confidence, probabilities
