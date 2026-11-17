"""The sidebar every page shows: counts and the engine's local state."""

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory

from mtg_gauntlet.context_processors import sidebar
from playtest.models import Match

from .factories import make_deck, make_match


@pytest.mark.django_db
def test_sidebar_counts_my_decks_playtests_and_running_matches(django_user_model, monkeypatch):
    monkeypatch.setenv("FORGE_URL", "http://forge:8080")
    user = django_user_model.objects.create_user(username="u")
    make_deck(user)
    make_match(user, status=Match.Status.RUNNING)
    make_match(django_user_model.objects.create_user(username="other"), status=Match.Status.RUNNING)
    request = RequestFactory().get("/")
    request.user = user
    data = sidebar(request)["sidebar"]
    assert (data["deck_count"], data["playtest_count"], data["running"]) == (1, 1, 1)
    assert data["forge_url"] == "http://forge:8080"


def test_sidebar_is_empty_for_anonymous_users():
    request = RequestFactory().get("/login")
    request.user = AnonymousUser()
    assert sidebar(request) == {"sidebar": {}}


@pytest.mark.parametrize("name, section", [
    ("home", "home"), ("home_running", "home"), ("deck_list", "decks"), ("deck_detail", "decks"),
    ("new_deck", "decks"), ("create_deck", "decks"), ("playtest_list", "playtests"),
    ("new_playtest", "playtests"), ("playtest_detail", "playtests"), ("match_detail", "playtests"),
    ("metagame", "metagame"), ("login", ""), (None, ""),
])
def test_nav_section(name, section):
    from mtg_gauntlet.templatetags.nav import nav_section

    assert nav_section(name) == section


@pytest.mark.django_db
def test_pages_show_the_sidebar_with_the_active_section(client, django_user_model):
    client.force_login(django_user_model.objects.create_user(username="u"))
    page = client.get("/decks").content.decode()
    assert 'class="sidebar"' in page and "MTG GAUNTLET" in page
    assert 'aria-current="page" href="/decks"' in page
    assert "fontawesome" not in page


@pytest.fixture
def forge_status(client, django_user_model, monkeypatch):
    """GET /engine/forge for a signed-in user, the bridge's health answered by ``health``."""
    from django.core.cache import cache

    from playtest import forge

    cache.clear()
    client.force_login(django_user_model.objects.create_user(username="u"))
    calls = []

    def get(health):
        class Bridge:
            def health(self, timeout):
                calls.append(timeout)
                if isinstance(health, Exception):
                    raise health
                return health

        monkeypatch.setattr(forge, "client", Bridge)
        return client.get("/engine/forge").content.decode()

    get.calls = calls
    return get


@pytest.mark.django_db
def test_the_sidebar_loads_the_engine_status_apart(client, django_user_model, monkeypatch):
    monkeypatch.setenv("FORGE_URL", "http://forge:8080")
    client.force_login(django_user_model.objects.create_user(username="u"))
    page = client.get("/decks").content.decode()
    assert 'hx-get="/engine/forge"' in page and "/engine/jev" not in page and "Jev" not in page
    assert page.count(">checking<") == 1 and 'title="http://forge:8080"' in page


@pytest.mark.django_db
@pytest.mark.parametrize("health, dot, label", [
    ({"status": "ready", "cards": 1}, "ok", "online"),
    ({"status": "loading"}, "warn", "loading"),
])
def test_the_forge_status_comes_from_its_health(forge_status, health, dot, label):
    row = forge_status(health)
    assert f'class="dot {dot}"' in row and f">{label}<" in row
    assert 'hx-trigger="every 30s"' in row and forge_status.calls == [2]


@pytest.mark.django_db
def test_an_unreachable_bridge_is_offline(forge_status):
    from playtest.forge import ForgeError

    row = forge_status(ForgeError("unreachable"))
    assert 'class="dot danger"' in row and ">offline<" in row


@pytest.mark.django_db
def test_the_forge_status_is_cached_a_few_seconds(forge_status):
    forge_status({"status": "ready"})
    assert ">online<" in forge_status({"status": "loading"})
    assert forge_status.calls == [2]


@pytest.mark.django_db
def test_the_matches_row_reloads_my_running_matches(client, django_user_model):
    user = django_user_model.objects.create_user(username="u")
    client.force_login(user)
    make_match(django_user_model.objects.create_user(username="other"), status=Match.Status.RUNNING)
    page = client.get("/decks").content.decode()
    assert 'hx-get="/engine/matches"' in page and ">0 running<" in page
    make_match(user, status=Match.Status.RUNNING)
    make_match(user, status=Match.Status.FINISHED)
    row = client.get("/engine/matches").content.decode()
    assert ">1 running<" in row and 'class="dot warn"' in row and 'hx-trigger="every 10s"' in row
