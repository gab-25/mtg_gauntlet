"""Tests for the Celery tasks (eager) and the worker's ready hook (called directly)."""

import pytest

from playtest import runner, tasks
from playtest.models import Match

from .test_runner import _match

pytestmark = pytest.mark.django_db

ENDED = [Match.Status.FINISHED, Match.Status.STOPPED, Match.Status.FAILED]


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="u")


def _status(match, status):
    Match.objects.filter(pk=match.pk).update(status=status)


def test_the_task_plays_a_pending_match_to_the_end(user, fake_forge):
    match = _match(user)
    tasks.run_match_task.delay(str(match.id))
    match.refresh_from_db()
    assert match.status == Match.Status.FINISHED, match.error
    assert tasks._following == set()


@pytest.mark.parametrize("status", ENDED)
def test_a_match_that_ended_is_left_alone(user, fake_forge, status):
    match = _match(user)
    _status(match, status)
    tasks.run_match_task.delay(str(match.id))
    match.refresh_from_db()
    assert match.status == status and fake_forge.created == []


def test_a_deleted_match_is_left_alone(user, fake_forge):
    match = _match(user)
    match_id = str(match.id)
    match.delete()
    tasks.run_match_task.delay(match_id)
    assert fake_forge.created == []


def test_a_match_already_followed_here_is_not_followed_twice(user, fake_forge, monkeypatch):
    match = _match(user)
    monkeypatch.setattr(tasks, "_following", {str(match.id)})
    tasks.run_match_task.delay(str(match.id))
    match.refresh_from_db()
    assert match.status == Match.Status.PENDING and fake_forge.created == []


def test_a_queued_match_stopped_before_its_turn_stays_stopped(user, fake_forge):
    match = _match(user)
    runner.start_game(match)
    Match.objects.filter(pk=match.pk).update(status=Match.Status.STOPPED, end_reason="aborted")
    tasks.run_match_task.delay(str(match.id))
    match.refresh_from_db()
    assert match.status == Match.Status.STOPPED and not match.events.exists()


def test_a_match_whose_worker_died_is_resumed_from_its_last_event(user, fake_forge):
    match = _match(user)
    runner.start_game(match)
    # The old worker copied the first turns of the log, then died.
    for _ in range(3):
        runner._store(match, fake_forge.events(match.forge_game_id, match.events.count())["events"])
    _status(match, Match.Status.RUNNING)
    copied = match.events.count()
    assert copied > 0
    tasks.run_match_task.delay(str(match.id))
    match.refresh_from_db()
    assert match.status == Match.Status.FINISHED, match.error
    seqs = list(match.events.order_by("seq").values_list("seq", flat=True))
    assert seqs == list(range(len(seqs))) and len(seqs) > copied


def test_recovery_requeues_only_pending_and_running_matches(user, monkeypatch):
    matches = {}
    for status in [Match.Status.PENDING, Match.Status.RUNNING, *ENDED]:
        matches[status] = _match(user)
        _status(matches[status], status)
    queued = []
    monkeypatch.setattr(tasks.run_match_task, "delay", queued.append)
    assert tasks.recover() == 2
    busy = [matches[Match.Status.PENDING], matches[Match.Status.RUNNING]]
    assert sorted(queued) == sorted(str(m.id) for m in busy)


def test_a_ready_worker_recovers(monkeypatch):
    calls = []
    monkeypatch.setattr(tasks, "recover", lambda: calls.append("recover") or 0)
    tasks.on_worker_ready(sender=None)
    assert calls == ["recover"]
