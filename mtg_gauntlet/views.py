"""The home page: what is running, what needs attention, how the decks are doing."""

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import Max
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from decks.formats import FORMATS
from decks.models import Deck, MetagameDeck
from decks.presentation import deck_pips
from playtest import forge
from playtest.models import Match, Playtest
from playtest.stats import needs_attention, record
from playtest.views import session_view

RECENT = 5
# The sidebar's Forge row loads apart from the page, asking the bridge with a
# short timeout and caching the answer, so a bridge that is down never slows
# the pages down.
FORGE_HEALTH_TIMEOUT = 2
FORGE_STATUS_TTL = 10
# Each state -> the sidebar's dot and label colour.
FORGE_STATES = {"online": "ok", "loading": "warn", "offline": "danger"}


def _playtests(user):
    return list(
        Playtest.objects.filter(owner=user).prefetch_related("matches__seats__metagame_deck", "matches__winner")
    )


def _running(playtests):
    """The most recent playtest still playing, as the running panel shows it."""
    for playtest in playtests:
        session = session_view(playtest)
        if session["busy"]:
            done = sum(row["result"] in ("won", "lost", "draw") for row in session["rows"])
            session["done"] = done
            session["total"] = len(session["rows"])
            session["percent"] = round(100 * done / session["total"]) if session["total"] else 0
            return session
    return None


@login_required
@require_http_methods(["GET"])
def home(request):
    playtests = _playtests(request.user)
    decks = list(Deck.objects.filter(owner=request.user))
    by_deck = {}
    for playtest in playtests:
        if playtest.deck_id:
            by_deck.setdefault(playtest.deck_id, []).append(playtest)
    rows = []
    for deck in decks:
        deck.pips = deck_pips(deck)
        tested = by_deck.get(deck.id, [])
        matches = [match for playtest in tested for match in playtest.matches.all()]
        rows.append({"deck": deck, "record": record(matches), "last": tested[0] if tested else None})
    synced = {
        row["format"]: row["synced"]
        for row in MetagameDeck.objects.values("format").annotate(synced=Max("fetched_at"))
    }
    recent = [
        {**session_view(playtest), "record": record(playtest.matches.all())} for playtest in playtests[:RECENT]
    ]
    return render(
        request,
        "home.html",
        {
            "running": _running(playtests),
            "deck_rows": rows,
            "notices": needs_attention(decks, synced, timezone.now()),
            "recent": recent,
            "formats": FORMATS.values(),
        },
    )


@login_required
@require_http_methods(["GET"])
def home_running(request):
    """The running panel, polled by HTMX while a playtest plays; empty once none does."""
    return render(request, "partials/running_now.html", {"running": _running(_playtests(request.user))})


def _forge_health() -> str:
    """"online", "loading" (Forge still reads its cards) or "offline"."""
    try:
        health = forge.client().health(timeout=FORGE_HEALTH_TIMEOUT)
    except forge.ForgeError:
        return "offline"
    return "online" if health.get("status") == "ready" else "loading"


def _forge_status() -> str:
    return cache.get_or_set("forge_status", _forge_health, FORGE_STATUS_TTL)


@login_required
@require_http_methods(["GET"])
def forge_status(request):
    """The sidebar's Forge bridge row, loaded apart from the page and polled by HTMX."""
    status = _forge_status()
    context = {"forge_url": forge.base_url(), "status": status, "dot": FORGE_STATES[status]}
    return render(request, "partials/forge_status.html", context)


@login_required
@require_http_methods(["GET"])
def matches_status(request):
    """The sidebar's Matches row: the user's running matches, polled by HTMX."""
    running = Match.objects.filter(owner=request.user, status=Match.Status.RUNNING).count()
    return render(request, "partials/matches_status.html", {"running": running})
