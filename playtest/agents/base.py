"""The interface between the Forge bridge and whoever plays a seat.

Forge lists the options of every decision; an agent only picks one of them, or
hands the decision back to Forge's AI. Agents never decide what is legal.
"""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Decision:
    # The index of the picked option, or None to let Forge's AI decide (delegate).
    choice: int | None
    # Why the agent chose it, in its own words (empty for agents that don't say).
    reasoning: str = ""
    # Set when the agent hands the decision to Forge's AI: why, in words, and
    # the kind of reason — "low_confidence", "error" or "budget".
    fallback_reason: str = ""
    fallback_kind: str = ""
    # For a model that answers with a distribution (Jev): how sure it was, the
    # top options with their probability, and the versioned model id.
    confidence: float | None = None
    probabilities: tuple[tuple[str, float], ...] = ()
    model: str = ""
    # On a simulated decision: Jev's score (0–6) of the position each option leads to, by label.
    scores: tuple[tuple[str, float], ...] = ()
    # The play the seat asked for just before, which Forge did not carry out:
    # the same decision came back, and that option was left out of it.
    failed_play: str = ""
    # What the agent saw and answered (state, options as shown, full
    # distribution, latency), stored as a DecisionTrace by the worker; never
    # sent to the bridge (#72).
    trace: dict | None = field(default=None, compare=False, repr=False)
    # The stored trace's id: the bridge keeps it on the event, linking the two.
    trace_id: int | None = None

    @property
    def delegate(self) -> bool:
        return self.choice is None

    def meta(self) -> dict:
        """The JSON-safe part of the decision the bridge stores on its event."""
        data = {}
        if self.confidence is not None:
            data["confidence"] = round(self.confidence, 4)
        if self.probabilities:
            data["probabilities"] = [{"label": label, "p": round(p, 4)} for label, p in self.probabilities]
        if self.scores:
            data["scores"] = [{"label": label, "score": round(score, 3)} for label, score in self.scores]
        if self.model:
            data["model"] = self.model
        if self.reasoning:
            data["reason"] = self.reasoning
        if self.fallback_reason:
            data["fallback_reason"] = self.fallback_reason
        if self.fallback_kind:
            data["fallback_kind"] = self.fallback_kind
        if self.failed_play:
            data["failed_play"] = self.failed_play
        if self.trace_id is not None:
            data["trace"] = self.trace_id
        return data


def delegate(kind: str, reason: str, **extra) -> Decision:
    """A decision handed to Forge's AI."""
    return Decision(None, fallback_kind=kind, fallback_reason=reason, **extra)


class Agent(Protocol):
    def choose(self, view: dict, options: list[dict], *, kind: str = "", prompt: str = "",
               simulated: bool = False) -> Decision:
        """Picks one of ``options`` given what the seat can see (``view``).

        All of it is the bridge's decision: the numbered ``options`` (``index``,
        ``label``, ``facts`` and, when ``simulated``, the ``outcome`` Forge
        simulated for each), the seat's filtered ``view``, the decision ``kind``
        (priority, attack, block, target…) and Forge's ``prompt``.
        """
        ...
