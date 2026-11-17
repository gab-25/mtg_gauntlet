"""Writes one match's events as JSON lines or Markdown."""

from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from playtest import export
from playtest.models import Match


class Command(BaseCommand):
    help = "Export a match for an offline review (to stdout unless --output)."

    def add_arguments(self, parser):
        parser.add_argument("match_id")
        parser.add_argument("--format", choices=sorted(export.FORMATS), default="jsonl")
        parser.add_argument("--seat", type=int, help="keep this seat's events (1-based) and the table-wide ones")
        parser.add_argument("--output", help="the file to write")

    def handle(self, *args, match_id, format, seat=None, output=None, **options):
        try:
            match = Match.objects.filter(pk=match_id).first()
        except ValidationError as exc:
            raise CommandError(f"No match {match_id}") from exc
        if match is None:
            raise CommandError(f"No match {match_id}")
        text = export.FORMATS[format](match, seat=None if seat is None else seat - 1)
        if output:
            Path(output).write_text(text)
        else:
            self.stdout.write(text, ending="")
