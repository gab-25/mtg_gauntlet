"""Suite-wide fixtures."""

import pytest
import requests


@pytest.fixture(autouse=True)
def no_network(request, monkeypatch):
    """The suite is hermetic: a request that reaches the transport fails the test.

    Tests stub ``requests.get`` / ``requests.post`` themselves; anything that
    slips past a stub lands here instead of on Scryfall, OpenRouter or the
    Forge bridge. Only the opt-in tests marked ``forge`` reach a real bridge.
    """
    if request.node.get_closest_marker("forge"):
        return

    def refuse(self, method, url, *args, **kwargs):
        raise AssertionError(f"unexpected network call in a test: {method} {url}")

    monkeypatch.setattr(requests.Session, "request", refuse)


@pytest.fixture(autouse=True)
def no_broker(monkeypatch):
    """Tasks run eagerly in tests: a message that would reach the broker fails the test."""
    from mtg_gauntlet.celery import app

    def refuse(*args, **kwargs):
        raise AssertionError("unexpected message to the Celery broker in a test")

    monkeypatch.setattr(app, "send_task", refuse)


@pytest.fixture
def fake_forge(monkeypatch):
    """Every bridge call goes to one in-memory :class:`~tests.fakeforge.FakeForge`."""
    from playtest import forge, runner

    from .fakeforge import FakeForge

    fake = FakeForge()
    monkeypatch.setattr(forge, "client", lambda: fake)
    # A fake game moves only when answered: no need to wait for more.
    monkeypatch.setattr(runner, "SELF_SERVE_WAIT", 0.01)
    return fake


@pytest.fixture
def simulating_forge(monkeypatch):
    """A fake bridge with ``SIMULATE=1``: attack, block and priority decisions are whole
    actions with outcomes, as :mod:`tests.fakeforge` fakes them."""
    from playtest import forge, runner

    from .fakeforge import FakeForge

    fake = FakeForge(simulate=True)
    monkeypatch.setattr(forge, "client", lambda: fake)
    monkeypatch.setattr(runner, "SELF_SERVE_WAIT", 0.01)
    return fake
