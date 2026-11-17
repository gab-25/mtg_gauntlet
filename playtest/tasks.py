"""The Celery side of a playtest: the match task and the worker's recovery.

:func:`run_match_task` follows one match's game (:func:`.runner.run_match`).
When the worker is ready it re-queues every match the database still calls
pending or running — a restart never leaves one behind, even if the broker lost
its queue. There is a single worker process (``--pool threads``), so
:data:`_following` is enough to drop a duplicate delivery.
"""

import logging
import threading

from celery import shared_task
from celery.signals import worker_ready

from . import runner
from .models import Match

logger = logging.getLogger(__name__)

BUSY = (Match.Status.PENDING, Match.Status.RUNNING)

# The matches a thread of this process is following.
_following: set[str] = set()
_following_lock = threading.Lock()


@shared_task(name="playtest.run_match")
def run_match_task(match_id: str) -> None:
    """Follows the match's game to its end. A match that ended (stopped while
    queued, say), was deleted or is already followed here is left alone."""
    key = str(match_id)
    with _following_lock:
        if key in _following or not Match.objects.filter(pk=key, status__in=BUSY).exists():
            return
        _following.add(key)
    try:
        runner.run_match(key)
    finally:
        with _following_lock:
            _following.discard(key)


def recover() -> int:
    """Queues every pending or running match; returns how many.

    Run when the worker starts, when no thread follows anything: a running
    match lost its follower, a pending one may have lost its message.
    """
    ids = [str(pk) for pk in Match.objects.filter(status__in=BUSY).values_list("pk", flat=True)]
    for match_id in ids:
        run_match_task.delay(match_id)
    if ids:
        logger.info("Re-queued %s matches left pending or running", len(ids))
    return len(ids)


@worker_ready.connect
def on_worker_ready(**kwargs) -> None:
    recover()
