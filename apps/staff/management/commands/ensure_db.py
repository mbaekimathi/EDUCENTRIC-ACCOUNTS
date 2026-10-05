"""Apply migrations and create any missing managed tables (idempotent)."""

from __future__ import annotations

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = (
        "Connect to MySQL and run migrations. Creates ACCOUNTS-owned tables when missing. "
        "Run after ADMINISTRATION ensure_db on a shared database. Safe on every deploy."
    )

    def handle(self, *args, **options):
        verbosity = options.get("verbosity", 1)
        self.stdout.write("Checking database connection…")
        connection.ensure_connection()
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")

        self.stdout.write("Applying migrations (creates tables if none exist)…")
        call_command("migrate", interactive=False, verbosity=verbosity)
        self.stdout.write(self.style.SUCCESS("ACCOUNTS database schema is up to date."))
