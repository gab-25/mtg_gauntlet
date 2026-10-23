"""Playtest views: set up a session, follow it and its matches, read a match's log."""

import json
import logging
import uuid
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import F, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from decks.formats import FORMATS, playable_formats
from decks.models import Deck, MetagameDeck
from decks.presentation import deck_pips
from mtg_gauntlet.access import get_owned_or_404

from . import forge
from .agents import typesafe
from .agents.jev_agent import filtered_state
from .forms import (
    MAX_GAMES,
    MAX_GAMES_PER_OPPONENT,
    MAX_OPPONENTS,
    METAGAME_PREFIX,
    AGENTS,
    DEFAULT_AGENT,
    parse_playtest_form,
)
from .models import DecisionTrace, Match, Playtest, Seat
from .runner import start_game
from .stats import flag_totals, opponent_source, record
from .summary import QUIET_LOG, event_row, fallback_executions, fallback_reason
from .tasks import run_match_task

logger = logging.getLogger(__name__)


def _grid_context(user, deck="", fmt="") -> dict:
    """Context for the playtest list, which polls itself while a match runs."""
    playtests = Playtest.objects.filter(owner=user).prefetch_related("matches__seats", "matches__winner")
    decks = list(Deck.objects.filter(owner=user, playtests__isnull=False).distinct().order_by("name"))
    deck_id = _uuid(deck)
    if deck_id is not None and any(d.id == deck_id for d in decks):
        playtests = playtests.filter(deck_id=deck_id)
    else:
        deck_id = None
    if fmt in FORMATS:
        playtests = playtests.filter(format=fmt)
    else:
        fmt = ""
    sessions = [{**session_view(playtest), "record": record(playtest.matches.all())} for playtest in playtests]
    return {
        "sessions": sessions,
        "filter_decks": decks,
        "filter_formats": [(key, f.label) for key, f in FORMATS.items()],
        "active_deck": str(deck_id) if deck_id else "",
        "active_format": fmt,
        "filtered": bool(deck_id or fmt),
        "has_busy": any(s["busy"] for s in sessions),
    }


def _uuid(value):
    """The UUID in ``value``, or None for anything else (a hand-edited query string)."""
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def session_view(playtest) -> dict:
    """A session with each match, its opponent and the tally of results, and
    the same matches grouped by opponent (``matchups``) with each one's record."""
    rows = []
    games = playtest.games_per_opponent or 1
    for match in sorted(playtest.matches.all(), key=lambda m: m.seed):
        seats = {seat.position: seat for seat in match.seats.all()}
        index, game = divmod(match.seed - playtest.seed, games)
        rows.append({"match": match, "opponent": seats.get(1), "result": match.result, "index": index, "game": game + 1})
    tally = {}
    for row in rows:
        tally[row["result"]] = tally.get(row["result"], 0) + 1
    return {
        "playtest": playtest,
        "rows": rows,
        "matchups": _matchups(rows),
        "tally": [(result, tally[result]) for result in RESULT_ORDER if result in tally],
        "busy": any(row["match"].is_busy for row in rows),
    }


def _matchups(rows) -> list[dict]:
    """The rows of each opponent, in the order the form listed them."""
    groups: dict[int, list] = {}
    for row in rows:
        groups.setdefault(row["index"], []).append(row)
    return [
        {
            "opponent": group[0]["opponent"],
            "rows": group,
            "record": record(row["match"] for row in group),
            **{result: sum(row["result"] == result for row in group) for result in ("running", "pending", "failed", "stopped")},
        }
        for _, group in sorted(groups.items())
    ]


# How a session's tally lists results.
RESULT_ORDER = ("won", "lost", "draw", "running", "pending", "stopped", "failed")


@login_required
@require_http_methods(["GET"])
def playtest_list(request):
    context = _grid_context(request.user, request.GET.get("deck", ""), request.GET.get("format", ""))
    template = "playtest/partials/playtest_grid.html" if request.htmx else "playtest/playtest_list.html"
    return render(request, template, context)


# "Top 5 of the metagame": opponents preselected when a format comes alone.
TOP_METAGAME = 5


def _form_context(user, data, preset_top=False, **extra) -> dict:
    """The new-playtest form for ``data`` (a query string or a refused POST).

    The deck to test sets the format; without one, ``format`` does, and
    without either no format is assumed: the opponent step waits for a deck.
    Only opponents of that format are offered, and only offered ones stay chosen.
    """
    decks = list(Deck.objects.filter(owner=user, status=Deck.Status.READY))
    for deck in decks:
        deck.pips = deck_pips(deck)
    deck_id = _uuid(data.get("deck"))
    deck = next((d for d in decks if d.id == deck_id), None)
    if deck is not None:
        fmt = deck.format
    else:
        fmt = data.get("format") if data.get("format") in playable_formats() else None
    metagame = [
        (f"{METAGAME_PREFIX}{meta.id}", meta)
        for meta in MetagameDeck.objects.filter(format=fmt, status=MetagameDeck.Status.READY).order_by("rank")
    ] if fmt else []
    mine = [(str(d.id), d) for d in decks if d.format == fmt]
    chosen = set(data.getlist("opponents")) if hasattr(data, "getlist") else set(data.get("opponents", []))
    # Only ?format= alone (the metagame page's "Test my deck against these").
    if preset_top and "deck" not in data and data.get("format") and not chosen:
        chosen = {value for value, _ in metagame[:TOP_METAGAME]}
    chosen &= {value for value, _ in metagame + mine}
    refused = [obj.archetype for value, obj in metagame if value in chosen and obj.unknown_cards]
    refused += [obj.name for value, obj in mine if value in chosen and obj.unknown_cards]
    playable = fmt is None or fmt in playable_formats()
    games = _games(data.get("games"))
    return {
        "decks": decks,
        "form_deck": deck,
        "form_format": fmt or "",
        "format_rules": FORMATS[fmt] if fmt else None,
        "playable": playable,
        "playable_labels": [f.label for f in playable_formats().values()],
        "metagame": metagame,
        "metagame_synced": max((meta.fetched_at for _, meta in metagame), default=None),
        "mine": mine,
        "form_opponents": chosen,
        "chosen_count": len(chosen),
        "refused": refused,
        "form_seed": data.get("seed", ""),
        "form_games": games,
        "form_agent": data.get("agent") if data.get("agent") in Seat.Agent.values else DEFAULT_AGENT,
        "agents": AGENTS,
        "chosen_games": len(chosen) * games,
        "max_opponents": MAX_OPPONENTS,
        "max_games_per_opponent": MAX_GAMES_PER_OPPONENT,
        "max_games": MAX_GAMES,
        "top_metagame": TOP_METAGAME,
        "jev_model": typesafe.MODEL,
        "jev_available": typesafe.api_key() is not None,
        **extra,
    }


def _games(value) -> int:
    """The games per opponent the form shows: a refused value comes back as typed when it is a number."""
    try:
        return max(1, int(str(value).strip()))
    except (TypeError, ValueError):
        return 1


@login_required
@require_http_methods(["GET"])
def new_playtest(request):
    # "Test this deck" passes ?deck=, "Run again" adds ?opponents=, the
    # metagame page passes ?format= alone and gets its top decks preselected.
    context = _form_context(request.user, request.GET, preset_top=True)
    return render(request, "playtest/playtest_new.html", context)


@login_required
@require_http_methods(["GET"])
def new_playtest_opponents(request):
    """The opponent step and the summary for the chosen deck (HTMX)."""
    context = _form_context(request.user, request.GET, oob=True)
    return render(request, "playtest/partials/opponents.html", context)


def _new_session(user, form) -> Playtest:
    """The session and ``form.games`` pending matches per opponent: the deck under test in seat 0."""
    with transaction.atomic():
        playtest = Playtest.objects.create(
            owner=user, format=form.format, deck=form.deck, deck_name=form.deck.name, seed=form.seed,
            games_per_opponent=form.games, agent=form.agent,
        )
        for index, opponent in enumerate(form.opponents):
            for game in range(form.games):
                seed = form.seed + index * form.games + game
                match = Match.objects.create(owner=user, playtest=playtest, format=form.format, seed=seed)
                Seat.objects.bulk_create([
                    Seat(match=match, position=0, deck=form.deck, deck_name=form.deck.name, agent=form.agent),
                    Seat(match=match, position=1, deck=opponent.deck, metagame_deck=opponent.metagame_deck,
                         deck_name=opponent.name, agent=form.agent),
                ])
    return playtest


@login_required
@require_http_methods(["POST"])
def create_playtest(request):
    """Starts a test session: the deck plays ``games`` matches against each opponent."""
    form, errors = parse_playtest_form(request.POST, request.user)
    if errors:
        context = _form_context(request.user, request.POST, errors=errors)
        return render(request, "playtest/playtest_new.html", context, status=422)

    playtest = _new_session(request.user, form)
    # Game by game across the opponents: the worker takes them in this order,
    # so every matchup gets going before any plays its second game.
    rows = sorted(session_view(playtest)["rows"], key=lambda row: (row["game"], row["index"]))
    matches = [row["match"] for row in rows]
    # Forge checks the decks now, with the first game of each opponent: a deck
    # it cannot play never becomes a session. The other games start when the
    # worker picks them up (runner.run_match).
    errors, status = [], 422
    for match in (row["match"] for row in rows if row["game"] == 1):
        try:
            start_game(match)
        except forge.ForgeDeckError as exc:
            errors += [f"{name}: Forge does not know {', '.join(cards)}." for name, cards in exc.unresolved.items()]
        except Exception as exc:  # noqa: BLE001 - a bridge error, or an answer we cannot read
            if not isinstance(exc, forge.ForgeError):
                logger.exception("Could not start the game")
            errors.append(f"The playtest could not start: {exc}")
            status = 503
            break
    if not errors:
        try:
            for match in matches:
                run_match_task.delay(str(match.id))
        except Exception as exc:  # noqa: BLE001 - the broker is unreachable or refused the task
            logger.exception("Could not queue the playtest")
            errors, status = [f"The playtest could not be queued: {exc}"], 503
    if errors:
        # A match queued before the failure finds its row deleted: its task leaves it alone.
        for match in matches:
            match.refresh_from_db()
            _abort(match)
        playtest.delete()
        context = _form_context(request.user, request.POST, errors=list(dict.fromkeys(errors)))
        return render(request, "playtest/playtest_new.html", context, status=status)
    return redirect("playtest_detail", playtest_id=playtest.id)


def _run_again_url(playtest, rows) -> str | None:
    """The new-playtest form filled with this session's deck and its opponents still around."""
    if playtest.deck_id is None:
        return None
    params = [("deck", playtest.deck_id), ("games", playtest.games_per_opponent), ("agent", playtest.agent)]
    for row in rows:
        seat = row["opponent"]
        if seat is None:
            continue
        if seat.metagame_deck_id:
            params.append(("opponents", f"{METAGAME_PREFIX}{seat.metagame_deck_id}"))
        elif seat.deck_id:
            params.append(("opponents", seat.deck_id))
    return f"{reverse('new_playtest')}?{urlencode(params)}"


def _playtest_context(playtest) -> dict:
    session = session_view(playtest)
    for row in session["rows"]:
        seats = {seat.position: seat for seat in row["match"].seats.all()}
        row["source"] = opponent_source(row["opponent"]) if row["opponent"] else ""
        lives = [seats.get(position) and seats[position].life for position in (0, 1)]
        row["life"] = " – ".join(str(life) for life in lives) if None not in lives else "—"
    for matchup in session["matchups"]:
        matchup["source"] = matchup["rows"][0]["source"]
        matchup["done"] = matchup["record"].played
    done = sum(row["result"] in ("won", "lost", "draw") for row in session["rows"])
    return {
        "session": session,
        "playtest": playtest,
        "record": record(playtest.matches.all()),
        "flag_totals": flag_totals(playtest.matches.all()),
        "done": done,
        "total": len(session["rows"]),
        "playing": sum(row["result"] == "running" for row in session["rows"]),
        "queued": sum(row["result"] == "pending" for row in session["rows"]),
        "run_again_url": _run_again_url(playtest, [matchup["rows"][0] for matchup in session["matchups"]]),
    }


@login_required
@require_http_methods(["GET"])
def playtest_detail(request, playtest_id):
    playtest = get_owned_or_404(Playtest, request.user, playtest_id)
    return render(request, "playtest/playtest_detail.html", _playtest_context(playtest))


@login_required
@require_http_methods(["GET"])
def playtest_live(request, playtest_id):
    """The session's results, polled by HTMX while a match runs."""
    playtest = get_owned_or_404(Playtest, request.user, playtest_id)
    return render(request, "playtest/partials/playtest_live.html", _playtest_context(playtest))


@login_required
@require_http_methods(["POST"])
def stop_playtest(request, playtest_id):
    playtest = get_owned_or_404(Playtest, request.user, playtest_id)
    for match in playtest.matches.all():
        _stop(match)
    return redirect("playtest_detail", playtest_id=playtest.id)


@login_required
@require_http_methods(["POST"])
def delete_playtest(request, playtest_id):
    playtest = get_owned_or_404(Playtest, request.user, playtest_id)
    for match in playtest.matches.all():
        if match.is_busy:
            _abort(match)
    playtest.delete()
    return redirect("playtest_list")





def log_rows(events, names: dict[int, str]) -> list[dict]:
    """The match log as the page shows it: Forge's narrative, the agents'
    decisions with their confidence, fallbacks with their reason, and runs of
    Forge-AI decisions folded into one line."""
    rows = []
    # A fallback's forge_ai twin is the same decision: the fallback line says it.
    executions = fallback_executions([event_row(event) for event in events])
    for index, event in enumerate(events):
        payload = event.payload or {}
        if index in executions or (event.kind == "log" and payload.get("type") in QUIET_LOG):
            continue
        if event.kind == "forge_ai":
            last = rows[-1] if rows else None
            if last and last["kind"] == "forge_ai" and last["seat"] == event.seat_position:
                last["count"] += 1
                method = payload.get("method", "")
                if method and method not in last["methods"]:
                    last["methods"].append(method)
                continue
            rows.append({
                "kind": "forge_ai", "seq": event.seq, "seat": event.seat_position, "who": names.get(event.seat_position, ""),
                "count": 1, "methods": [payload.get("method", "")], "event": event,
            })
            continue
        row = {"kind": event.kind, "seq": event.seq, "seat": event.seat_position, "who": names.get(event.seat_position, ""), "event": event}
        if event.kind == "log":
            row["headline"] = payload.get("type") in ("TURN", "GAME_OUTCOME")
        elif event.kind == "fallback":
            row["reason"] = fallback_reason(payload)
        rows.append(row)
    return rows


# The match page's tabs (``?tab=``) and the log's filters (``?show=``).
MATCH_TABS = ("summary", "log", "decisions")
LOG_FILTERS = ("all", "key", "fallbacks")


def _shown(rows, show: str) -> list[dict]:
    """The log rows a filter keeps: key moments are turns and the outcome."""
    if show == "key":
        return [row for row in rows if row.get("headline") or row["kind"] == "game_over"]
    if show == "fallbacks":
        return [row for row in rows if row["kind"] == "fallback"]
    return rows


# The decision inspector's filters (``?only=``), besides ``?seat=`` (1-based) and ``?kind=``.
DECISION_FILTERS = (
    ("all", "Every decision"),
    ("fallbacks", "Forge AI decided"),
    ("flagged", "Flagged"),
    ("no_effect", "No effect"),
    ("disagree", "Disagrees with Forge AI"),
)


def _decisions(match, seat: str = "", kind: str = "", only: str = "") -> list:
    """The match's decision traces the inspector's filters keep, in order."""
    traces = match.traces.all()
    if seat.isdigit():
        traces = traces.filter(seat_position=int(seat) - 1)
    if kind:
        traces = traces.filter(kind=kind)
    if only == "fallbacks":
        traces = traces.filter(Q(choice__isnull=True) | Q(outcome=DecisionTrace.Outcome.DELEGATED))
    elif only == "no_effect":
        traces = traces.filter(outcome=DecisionTrace.Outcome.NO_EFFECT)
    elif only == "disagree":
        traces = (
            traces.filter(choice__isnull=False, forge_ai_choice__isnull=False)
            .exclude(outcome=DecisionTrace.Outcome.DELEGATED)
            .exclude(choice=F("forge_ai_choice"))
        )
    traces = list(traces)
    if only == "flagged":
        traces = [trace for trace in traces if trace.flags]
    return traces


def _filter_query(seat: str, kind: str, only: str) -> str:
    return urlencode([(key, value) for key, value in (("seat", seat), ("kind", kind), ("only", only)) if value and value != "all"])


def _decision_filters(params) -> dict:
    """The inspector's filters from a query string, and the query that keeps them."""
    seat, kind, only = params.get("seat", ""), params.get("kind", ""), params.get("only", "")
    seat = seat if seat.isdigit() else ""
    only = only if only in dict(DECISION_FILTERS) else "all"
    return {"seat_filter": seat, "kind_filter": kind, "only": only, "decision_query": _filter_query(seat, kind, only)}


def _decision_chips(match, filters: dict) -> dict:
    """The inspector's filter chips: ``(label, query, active)``; a seat or kind chip toggles."""
    seat, kind, only = filters["seat_filter"], filters["kind_filter"], filters["only"]
    seats = [str(position + 1) for position in match.seats.values_list("position", flat=True)]
    kinds = sorted(set(match.traces.values_list("kind", flat=True)))
    return {
        "only_chips": [
            (label, _filter_query(seat, kind, value), value == only)
            for value, label in DECISION_FILTERS
            # Only a bridge that says what Forge's AI would pick (#75) can disagree.
            if value != "disagree" or value == only or match.traces.exclude(forge_ai_choice=None).exists()
        ],
        "seat_chips": [(f"Seat {s}", _filter_query("" if s == seat else s, kind, only), s == seat) for s in seats],
        "kind_chips": [(k, _filter_query(seat, "" if k == kind else k, only), k == kind) for k in kinds],
    }


def _live_context(match, tab="summary", show="all", params=None) -> dict:
    tab = tab if tab in MATCH_TABS else "summary"
    show = show if show in LOG_FILTERS else "all"
    seats = list(match.seats.select_related("metagame_deck"))
    summary = match.summary or {}
    by_seat = {s["seat"]: s for s in summary.get("seats", [])}
    events = list(match.events.all())
    siblings = list(match.playtest.matches.order_by("seed").values_list("id", "seed"))
    later = [match_id for match_id, seed in siblings if seed > match.seed]
    opponent = next((seat for seat in seats if seat.position == 1), None)
    filters = _decision_filters(params or {})
    inspecting = tab == "decisions"
    return {
        "match": match,
        "playtest": match.playtest,
        "seats": seats,
        "opponent": opponent,
        "sources": {seat.position: opponent_source(seat) for seat in seats},
        "event_count": len(events),
        "log": _shown(log_rows(events, {seat.position: seat.deck_name for seat in seats}), show),
        "summary": summary,
        "seat_stats": [(seat, by_seat.get(seat.position)) for seat in seats],
        "jev_model": typesafe.MODEL,
        "tab": tab,
        "show": show,
        "log_filters": (("all", "Everything"), ("key", "Key moments"), ("fallbacks", "Fallbacks to Forge AI")),
        "next_match_id": later[0] if later else None,
        "decisions": _decisions(match, filters["seat_filter"], filters["kind_filter"], filters["only"]) if inspecting else [],
        "has_traces": inspecting and match.traces.exists(),
        **(_decision_chips(match, filters) if inspecting else {}),
        **filters,
    }


def _match_or_404(user, playtest_id, match_id) -> Match:
    """The user's match, found only under its own playtest."""
    match = get_owned_or_404(Match, user, match_id)
    if match.playtest_id != playtest_id:
        raise Http404("No such match in this playtest")
    return match


@login_required
@require_http_methods(["GET"])
def match_detail(request, playtest_id, match_id):
    match = _match_or_404(request.user, playtest_id, match_id)
    context = _live_context(match, request.GET.get("tab", ""), request.GET.get("show", ""), request.GET)
    return render(request, "playtest/match_detail.html", context)


@login_required
@require_http_methods(["GET"])
def match_live(request, playtest_id, match_id):
    """The scoreboard and log region, polled by HTMX while the match runs."""
    match = _match_or_404(request.user, playtest_id, match_id)
    context = _live_context(match, request.GET.get("tab", ""), request.GET.get("show", ""), request.GET)
    return render(request, "playtest/partials/match_live.html", context)


@login_required
@require_http_methods(["GET"])
def decision_detail(request, playtest_id, match_id, trace_id):
    """One decision as Jev saw it: the state, Forge's prompt, every option with its facts and probability."""
    match = _match_or_404(request.user, playtest_id, match_id)
    trace = get_object_or_404(DecisionTrace, pk=trace_id, match=match)
    filters = _decision_filters(request.GET)
    siblings = [t.id for t in _decisions(match, filters["seat_filter"], filters["kind_filter"], filters["only"])]
    position = siblings.index(trace.id) if trace.id in siblings else None
    seats = {seat.position: seat for seat in match.seats.all()}
    options = []
    for option in trace.options:
        p = trace.probabilities.get(str(option["index"]))
        options.append({
            **option,
            "p": p,
            "percent": round(100 * (p or 0)),
            "chosen": option["index"] == trace.choice,
            "forge_ai": option["index"] == trace.forge_ai_choice,
        })
    return render(request, "playtest/decision_detail.html", {
        "match": match,
        "playtest": match.playtest,
        "trace": trace,
        "seat": seats.get(trace.seat_position),
        "options": options,
        "state": filtered_state(trace.state or {}),
        "state_json": json.dumps(trace.state, indent=2, ensure_ascii=False),
        "previous_id": siblings[position - 1] if position else None,
        "next_id": siblings[position + 1] if position is not None and position + 1 < len(siblings) else None,
        **filters,
    })


def _abort(match) -> None:
    """Ends the match's game on the bridge; a bridge that is gone has nothing to end."""
    if not match.forge_game_id:
        return
    try:
        forge.client().abort(match.forge_game_id)
    except forge.ForgeError as exc:
        logger.warning("Could not abort game %s: %s", match.forge_game_id, exc)


def _stop(match) -> None:
    """Stops a running match: the bridge aborts the game and the runner records it."""
    if not match.is_busy:
        return
    _abort(match)
    # The runner records the end when it follows the game. A match still in
    # the queue, or whose worker restarted, is marked here: its task reads
    # this row and leaves the match alone.
    Match.objects.filter(pk=match.pk, status__in=[Match.Status.PENDING, Match.Status.RUNNING]).update(
        status=Match.Status.STOPPED, end_reason="aborted", finished_at=timezone.now()
    )


@login_required
@require_http_methods(["POST"])
def stop_match(request, playtest_id, match_id):
    match = _match_or_404(request.user, playtest_id, match_id)
    _stop(match)
    return redirect("match_detail", playtest_id=playtest_id, match_id=match.id)


@login_required
@require_http_methods(["POST"])
def delete_match(request, playtest_id, match_id):
    match = _match_or_404(request.user, playtest_id, match_id)
    if match.is_busy:
        _abort(match)
    playtest_id = match.playtest_id
    match.delete()
    return redirect("playtest_detail", playtest_id=playtest_id)
