"""The migrations: one initial migration per app, in step with the models."""

from io import StringIO

import pytest
from django.core.management import call_command


@pytest.mark.django_db
def test_the_migrations_match_the_models():
    out = StringIO()
    call_command("makemigrations", "--check", "--dry-run", stdout=out)
