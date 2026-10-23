"""Matches between AI agents, their seats and what happened in them."""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from decks.formats import DEFAULT_FORMAT, FORMATS, format_choices

# Forge's reasons for a game's end (its GameEndReason, plus the bridge's own),
# as the match page names them.
END_REASONS = {
    "AllOpponentsLost": "All opponents lost",
    "Draw": "Draw",
    "Conceded": "Conceded",
    "WinsGameSpellEffect": "Won by a card's effect",
    "turn_limit": "Turn limit reached",
    "aborted": "Stopped",
    "orphaned": "No worker answered",
}


class Playtest(models.Model):
    """A test session: one deck plays ``games_per_opponent`` matches against each chosen opponent."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="playtests"
    )
    format = models.CharField(max_length=16, choices=format_choices(), default=DEFAULT_FORMAT)
    # The deck under test; deleting it keeps the session, ``deck_name`` remembers it.
    deck = models.ForeignKey(
        "decks.Deck", null=True, blank=True, on_delete=models.SET_NULL, related_name="playtests"
    )
    deck_name = models.CharField(max_length=255)
    # Game ``g`` (0-based) against opponent ``o`` is seeded with
    # ``seed + o * games_per_opponent + g``: unique in the session, and the
    # same seeds as before with one game per opponent.
    seed = models.BigIntegerField()
    # Independent game 1s against each opponent; Forge's seeded coin toss
    # picks who plays first in each.
    games_per_opponent = models.PositiveSmallIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "playtests"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.deck_name} ({self.format})"


class Match(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        FINISHED = "finished", "Finished"
        FAILED = "failed", "Failed"
        STOPPED = "stopped", "Stopped"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="matches"
    )
    # The test session this match belongs to: seat 0 plays the deck under test,
    # seat 1 one of its opponents.
    playtest = models.ForeignKey(Playtest, on_delete=models.CASCADE, related_name="matches")
    format = models.CharField(max_length=16, choices=format_choices(), default=DEFAULT_FORMAT)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    # Seeds Forge's shuffles: the same seed and the same answers replay the same game.
    seed = models.BigIntegerField()
    # The game on the Forge bridge, set once the bridge accepted the decks.
    forge_game_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    # The game turn in progress (both players' turns count), updated as the log arrives.
    turn = models.PositiveIntegerField(default=0)

    winner = models.ForeignKey(
        "Seat", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # Forge's reason (see END_REASONS), kept verbatim.
    end_reason = models.CharField(max_length=32, blank=True, default="")
    error = models.TextField(blank=True, default="")
    # The deterministic match summary (playtest.summary.summarize_match),
    # written when the game ends; empty until then.
    summary = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "matches"
        ordering = ["-created_at"]
        verbose_name_plural = "matches"

    @property
    def is_busy(self) -> bool:
        return self.status in {self.Status.PENDING, self.Status.RUNNING}

    @property
    def has_commander(self) -> bool:
        return FORMATS[self.format].has_commander

    def get_end_reason_display(self) -> str:
        return END_REASONS.get(self.end_reason, self.end_reason)

    @property
    def result(self) -> str:
        """For the deck in seat 0: ``won``, ``lost``, ``draw``, or the match's status while it is not finished."""
        if self.status != self.Status.FINISHED:
            return self.status
        if self.winner_id is None:
            return "draw"
        return "won" if self.winner.position == 0 else "lost"


class Seat(models.Model):
    class Agent(models.TextChoices):
        # Older matches only: new seats are always played by an agent.
        FORGE_AI = "forge_ai", "Forge AI"
        JEV = "jev", "Jev"

    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="seats")
    # 0-based place at the table.
    position = models.PositiveSmallIntegerField()
    # Deleting a deck keeps the matches it played; ``deck_name`` remembers it.
    deck = models.ForeignKey(
        "decks.Deck", null=True, blank=True, on_delete=models.SET_NULL, related_name="seats"
    )
    deck_name = models.CharField(max_length=255)
    # A metagame deck (decks.MetagameDeck) this seat plays; a later sync may
    # delete it, and ``deck_name`` keeps its name.
    metagame_deck = models.ForeignKey(
        "decks.MetagameDeck", null=True, blank=True, on_delete=models.SET_NULL, related_name="seats"
    )
    agent = models.CharField(max_length=16, choices=Agent.choices, default=Agent.JEV)
    # Jev model id of an older seat; empty means typesafe.MODEL.
    model = models.CharField(max_length=128, blank=True, default="")
    # What Forge made of the deck when the game was created:
    # ``{"unsupported_cards": [...]}``, cards it knows but cannot play fully.
    coverage = models.JSONField(default=dict, blank=True)

    # The starting life once the match runs, then kept current by its events.
    life = models.IntegerField(null=True, blank=True)

    class Meta:
        db_table = "seats"
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["match", "position"], name="unique_seat_position"),
        ]

    def __str__(self) -> str:
        return f"Seat {self.position + 1}: {self.deck_name}"

    @property
    def agent_icon(self) -> str:
        """The icon-font ligature for this seat's agent."""
        return {
            self.Agent.FORGE_AI: "memory",
            self.Agent.JEV: "bolt",
        }.get(self.agent, "bolt")

    @property
    def controller(self) -> str:
        """How the bridge seats it: Forge's AI plays it, or a worker answers for it."""
        return "forge_ai" if self.agent == self.Agent.FORGE_AI else "external"


class MatchEvent(models.Model):
    """One entry of a match log, as the Forge bridge reported it.

    ``kind`` is the bridge's: ``log`` (Forge's game log; ``payload.type`` is
    its category), ``decision``, ``fallback``, ``forge_ai`` or ``game_over``.
    """

    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="events")
    seq = models.PositiveIntegerField()
    turn = models.PositiveIntegerField()
    # The seat the event is about, or None for table-wide events.
    seat_position = models.PositiveSmallIntegerField(null=True, blank=True)
    kind = models.CharField(max_length=32)
    # Forge's phase it happened in (MAIN1, COMBAT_DECLARE_ATTACKERS…), when known.
    step = models.CharField(max_length=32, blank=True, default="")
    text = models.TextField()
    payload = models.JSONField(default=dict)
    reasoning = models.TextField(blank=True, default="")

    class Meta:
        db_table = "match_events"
        ordering = ["seq"]
        constraints = [
            models.UniqueConstraint(fields=["match", "seq"], name="unique_event_seq"),
        ]


class DecisionTrace(models.Model):
    """What a seat's agent saw and answered for one decision it was asked (#72).

    One row per decision that reached Jev — answered or failed — never for
    budget or forced passes. ``options`` are in the
    order Jev was shown them; ``probabilities`` map an option's index (as a
    string) to Jev's probability. The runner links ``event`` and settles
    ``outcome`` (the worker marks ``no_effect`` as soon as it sees the same
    decision come back); the checks of :mod:`.checks` fill ``flags``.
    """

    class Outcome(models.TextChoices):
        PENDING = "", "Pending"
        DONE = "done", "Done"
        NO_EFFECT = "no_effect", "No effect"
        DELEGATED = "delegated", "Delegated to Forge AI"

    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="traces")
    seat_position = models.PositiveSmallIntegerField()
    event = models.ForeignKey(MatchEvent, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decision_id = models.CharField(max_length=64, blank=True, default="")
    turn = models.PositiveIntegerField(default=0)
    step = models.CharField(max_length=32, blank=True, default="")
    kind = models.CharField(max_length=32, blank=True, default="")
    prompt = models.TextField(blank=True, default="")
    instructions = models.TextField(blank=True, default="")
    state = models.JSONField(default=dict, blank=True)
    options = models.JSONField(default=list, blank=True)
    probabilities = models.JSONField(default=dict, blank=True)
    confidence = models.FloatField(null=True, blank=True)
    # The picked option's index; None when Forge's AI decided.
    choice = models.SmallIntegerField(null=True, blank=True)
    fallback_kind = models.CharField(max_length=32, blank=True, default="")
    fallback_reason = models.TextField(blank=True, default="")
    failed_play = models.TextField(blank=True, default="")
    model = models.CharField(max_length=128, blank=True, default="")
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    outcome = models.CharField(max_length=16, choices=Outcome.choices, blank=True, default=Outcome.PENDING)
    # [{"check", "reason"}] from playtest.checks, written when the match ends.
    flags = models.JSONField(default=list, blank=True)
    # The option Forge's AI would have picked, when the bridge says (#75).
    forge_ai_choice = models.SmallIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "decision_traces"
        ordering = ["id"]
        indexes = [models.Index(fields=["match", "seat_position"], name="trace_match_seat")]

    def option(self, index):
        """The shown option with ``index``, or None."""
        return next((option for option in self.options if option.get("index") == index), None)

    @property
    def chosen_label(self) -> str:
        option = self.option(self.choice)
        return option["label"] if option else ""
