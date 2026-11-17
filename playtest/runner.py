"""Plays a stored match on the Forge bridge and stores what happened.

:func:`start_game` sends the seats' decklists to the bridge, which refuses a
deck with a card Forge does not know (:class:`~.forge.ForgeDeckError`) before
any game starts; the new-playtest view calls it, so a refused match is never
created. :func:`run_match`, the background job, then copies the bridge's
events into ``MatchEvent`` rows as they arrive and, when the game is over,
stores the winner, the end reason and the summary. Forge's AI plays every
seat: nothing here answers the game.
"""

import logging
import time

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from decks.formats import FORMATS
from mtg_gauntlet.logging_context import job_log_context

from . import forge
from .models import Match, MatchEvent, Seat
from .summary import current_lives, summarize_match, table_names

logger = logging.getLogger(__name__)

# Pause between two reads of the log when nothing happened.
POLL_SECONDS = 1.0
# How long the bridge may stay unreachable while a game is followed (a
# restart of the web container, a network blip) before the match fails.
MAX_OUTAGE_SECONDS = 60
# A game the bridge never ends is aborted after this long (the bridge's own
# turn cap normally ends it much sooner).
DEFAULT_MATCH_TIMEOUT = 3 * 3600


def seat_cards(seat: Seat) -> list:
    """The stored cards a seat plays: a metagame deck, or the user's deck."""
    if seat.metagame_deck_id is not None:
        if seat.metagame_deck is None:
            raise forge.ForgeError(f"The metagame deck of seat {seat.position + 1} was replaced by a newer sync.")
        return seat.metagame_deck.cards
    if seat.deck is None:
        raise forge.ForgeError(f"The deck of seat {seat.position + 1} was deleted.")
    return seat.deck.cards


def _remember_metagame_coverage(seat: Seat, unresolved: list, unsupported: list) -> None:
    """Keeps what Forge said of a seat's metagame deck: the sync does not ask it."""
    if seat.metagame_deck is not None:
        seat.metagame_deck.coverage = {"unresolved": unresolved, "unsupported_cards": unsupported}
        seat.metagame_deck.save(update_fields=["coverage"])


def start_game(match: Match, client=None) -> dict:
    """Creates the match's game on the bridge; stores its id and each seat's coverage.

    What the bridge says of a metagame deck is kept on it too. Raises
    :class:`~.forge.ForgeDeckError` (unknown cards, named per seat) or
    :class:`~.forge.ForgeError`; then only the unknown cards are stored.
    """
    client = client or forge.client()
    seats = list(match.seats.select_related("deck", "metagame_deck"))
    has_commander = FORMATS[match.format].has_commander
    names = table_names(seats)
    try:
        game = client.create_game(
            match.format,
            match.seed,
            [
                {
                    "name": name,
                    "decklist": forge.decklist_text(seat_cards(seat), has_commander=has_commander),
                }
                for seat, name in zip(seats, names)
            ],
        )
    except forge.ForgeDeckError as exc:
        # Seats the error does not name may still hold unknown cards: the
        # bridge can stop at the first bad deck, so only named seats are kept.
        for seat, name in zip(seats, names):
            if exc.unresolved.get(name):
                _remember_metagame_coverage(seat, list(exc.unresolved[name]), [])
        raise
    with transaction.atomic():
        for seat, bridge_seat in zip(seats, game.get("seats", [])):
            unsupported = list(bridge_seat.get("unsupported_cards") or [])
            seat.coverage = {"unsupported_cards": unsupported}
            seat.save(update_fields=["coverage"])
            _remember_metagame_coverage(seat, [], unsupported)
        match.forge_game_id = game["id"]
        match.save(update_fields=["forge_game_id"])
    return game


def _store(match: Match, events: list[dict]) -> None:
    MatchEvent.objects.bulk_create(
        [
            MatchEvent(
                match_id=match.pk,
                seq=event["seq"],
                turn=event.get("turn") or 0,
                step=(event.get("phase") or "")[:32],
                seat_position=event.get("seat"),
                kind=event["kind"][:32],
                text=event.get("text") or "",
                payload=event.get("payload") or {},
            )
            for event in events
        ],
        ignore_conflicts=True,
    )
    turn = max((event.get("turn") or 0 for event in events), default=0)
    if turn > match.turn:
        match.turn = turn
        Match.objects.filter(pk=match.pk).update(turn=turn)
    # Each seat's life follows the game as it goes; _finish stores the final one.
    if any(event["kind"] == "log" and (event.get("payload") or {}).get("type") == "LIFE" for event in events):
        for position, life in current_lives(events, list(match.seats.all())).items():
            Seat.objects.filter(match_id=match.pk, position=position).update(life=life)


def _match_timeout() -> float:
    return float(getattr(settings, "MATCH_TIMEOUT", DEFAULT_MATCH_TIMEOUT))


def follow(match: Match, client) -> dict:
    """Copies the game's events until the bridge ends it; returns the final game."""
    last = match.events.order_by("-seq").values_list("seq", flat=True).first()
    since = 0 if last is None else last + 1
    deadline = time.monotonic() + _match_timeout()
    outage_since = None
    while True:
        try:
            batch = client.events(match.forge_game_id, since)
            outage_since = None
            if batch["events"]:
                _store(match, batch["events"])
                since = batch["next"]
                continue
            game = client.game(match.forge_game_id)
        except forge.ForgeGameNotFound:
            raise  # no outage to wait out: the game is gone
        except forge.ForgeError:
            outage_since = outage_since or time.monotonic()
            if time.monotonic() - outage_since > MAX_OUTAGE_SECONDS:
                raise
            time.sleep(POLL_SECONDS)
            continue
        if game["status"] in forge.ENDED:
            # The last events may have landed after the read above; a game
            # that ended is recorded even if that read fails.
            try:
                _store(match, client.events(match.forge_game_id, since)["events"])
            except forge.ForgeError as exc:
                logger.warning("Could not read the last events: %s", exc)
            return game
        if not Match.objects.filter(pk=match.pk).exists():
            client.abort(match.forge_game_id)
            raise forge.ForgeError("The match was deleted while it ran.")
        if time.monotonic() > deadline:
            client.abort(match.forge_game_id)
            raise forge.ForgeError(f"The game did not end within {_match_timeout():.0f} seconds; it was aborted.")
        time.sleep(POLL_SECONDS)


def _fail(match_id, error: str, client=None, game_id: str = "") -> None:
    """Marks the match failed and ends its game, so Forge stops playing it."""
    Match.objects.filter(pk=match_id).update(status=Match.Status.FAILED, error=error, finished_at=timezone.now())
    if client is not None and game_id:
        try:
            client.abort(game_id)
        except forge.ForgeError as exc:
            logger.warning("Could not abort game %s: %s", game_id, exc)


def _finish(match: Match, game: dict) -> None:
    seats = list(match.seats.all())
    events = list(match.events.all())
    summary = summarize_match(events, seats, match.format, game)
    status = game["status"]
    error = ""
    if status == "failed":
        result, error = Match.Status.FAILED, f"Forge failed: {game.get('error') or 'unknown error'}"
    elif status == "aborted":
        result = Match.Status.STOPPED
    else:
        result = Match.Status.FINISHED
    by_position = {seat.position: seat for seat in seats}
    with transaction.atomic():
        for stats in summary["seats"]:
            seat = by_position.get(stats["seat"])
            if seat is not None:
                seat.life = stats["final_life"]
                seat.save(update_fields=["life"])
        Match.objects.filter(pk=match.pk).update(
            status=result,
            error=error,
            turn=max(game.get("turn") or 0, match.turn),
            winner=by_position.get(game.get("winner")),
            end_reason=(game.get("end_reason") or "")[:32],
            summary=summary,
            finished_at=timezone.now(),
        )
    logger.info("Match %s: %s, %s", result, game.get("end_reason"), game.get("winner"))


def run_match(match_id) -> None:
    """Celery task body (:func:`.tasks.run_match_task`): follows ``match_id``'s game on the bridge to its end."""
    with job_log_context("match", match_id):
        match = Match.objects.filter(pk=match_id).first()
        if match is None:
            return  # deleted before the job started
        client = forge.client()
        try:
            if not match.forge_game_id:
                start_game(match, client)
            match.started_at = match.started_at or timezone.now()
            Match.objects.filter(pk=match_id).update(status=Match.Status.RUNNING, started_at=match.started_at)
            match.seats.filter(life__isnull=True).update(life=FORMATS[match.format].starting_life)
            game = follow(match, client)
            if Match.objects.filter(pk=match_id).exists():
                _finish(match, game)
        except forge.ForgeError as exc:
            logger.warning("Match failed: %s", exc)
            _fail(match_id, str(exc), client, match.forge_game_id)
        except Exception as exc:  # noqa: BLE001 - record any failure for the user.
            logger.exception("Match failed")
            _fail(match_id, str(exc), client, match.forge_game_id)
