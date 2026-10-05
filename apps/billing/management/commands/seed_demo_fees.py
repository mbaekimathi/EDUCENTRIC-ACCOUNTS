"""
Seed demo fee charges and payments for Best Kenya College learners.

Requires ADMINISTRATION seed first (students with assessment ASM1001–ASM1010).

  python manage.py seed_demo_fees
  python manage.py seed_demo_fees --password DemoPass123!
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from apps.billing.models import FeeCategory, FeeCharge, Payment, allocate_payment_to_charges


DEFAULT_PASSWORD = "DemoPass123!"

ASSESSMENT_NUMBERS = [f"ASM{1001 + i}" for i in range(10)]

CATEGORY_DEFAULTS = [
    ("TUIT", "Tuition", "Core academic fees"),
    ("TRAN", "Transport", "School transport"),
    ("LUNC", "Lunch", "Meals programme"),
    ("EXAM", "Examination", "Internal and external exam fees"),
    ("UNIF", "Uniform", "School uniform"),
    ("OTHR", "Other", "Miscellaneous charges"),
    ("BOOK", "Books", "Textbooks and workbooks"),
    ("ACTV", "Activity", "Clubs and co-curricular"),
    ("LAB", "Laboratory", "Science lab fees"),
    ("DEV", "Development", "Infrastructure levy"),
]

# 10 charge templates (one primary charge style per student index).
CHARGE_AMOUNTS = [
    Decimal("25000.00"),  # tuition-heavy
    Decimal("4500.00"),
    Decimal("3000.00"),
    Decimal("1500.00"),
    Decimal("5500.00"),
    Decimal("2000.00"),
    Decimal("3500.00"),
    Decimal("4000.00"),
    Decimal("2800.00"),
    Decimal("5000.00"),
]


def _lookup_student_ids_by_assessment(numbers: list[str]) -> dict[str, int]:
    """Resolve admissions_student.id via shared MySQL (no ORM FK in Accounts)."""
    if connection.vendor != "mysql" and connection.vendor != "sqlite":
        # Still works on SQLite local if shared DB.
        pass
    placeholders = ", ".join(["%s"] * len(numbers))
    sql = (
        f"SELECT id, assessment_number FROM admissions_student "
        f"WHERE assessment_number IN ({placeholders})"
    )
    with connection.cursor() as cursor:
        cursor.execute(sql, numbers)
        rows = cursor.fetchall()
    return {str(assess): int(pk) for pk, assess in rows}


class Command(BaseCommand):
    help = (
        "Seed 10 fee categories (extend bootstrap), 10 charges and 10 payments "
        "for Best Kenya College demo students (idempotent)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--academic-year",
            default="2026",
            help="Academic year label on charges (default: 2026)",
        )
        parser.add_argument(
            "--term",
            default="Term 1",
            help="Term label on charges (default: Term 1)",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        year = options["academic_year"]
        term = options["term"]

        self.stdout.write("Seeding Best Kenya College fee demo data…")

        categories = []
        for code, name, description in CATEGORY_DEFAULTS:
            cat, made = FeeCategory.objects.get_or_create(
                code=code,
                defaults={"name": name, "description": description, "is_active": True},
            )
            if not made and cat.name != name:
                cat.name = name
                cat.description = description
                cat.is_active = True
                cat.save(update_fields=["name", "description", "is_active"])
            categories.append(cat)
            if made:
                self.stdout.write(self.style.SUCCESS(f"Fee category {code} created"))

        try:
            student_map = _lookup_student_ids_by_assessment(ASSESSMENT_NUMBERS)
        except Exception as exc:
            raise CommandError(
                "Could not read admissions_student. Run Admin seed first "
                f"and confirm shared DB. ({exc})"
            ) from exc

        missing = [n for n in ASSESSMENT_NUMBERS if n not in student_map]
        if missing:
            raise CommandError(
                "Missing students for assessments: "
                + ", ".join(missing)
                + ". Run in ADMINISTRATION: python manage.py seed_best_kenya_college"
            )

        due = date.today() + timedelta(days=21)
        charges_made = 0
        payments_made = 0

        for i, assess in enumerate(ASSESSMENT_NUMBERS):
            student_id = student_map[assess]
            category = categories[i]
            amount = CHARGE_AMOUNTS[i]
            title = f"{category.name} — Best Kenya College {term} {year}"

            charge, created = FeeCharge.objects.get_or_create(
                student_id=student_id,
                category=category,
                academic_year=year,
                term=term,
                title=title,
                defaults={
                    "amount": amount,
                    "amount_paid": Decimal("0.00"),
                    "status": FeeCharge.Status.OPEN,
                    "due_date": due,
                    "notes": f"Demo charge for {assess}",
                },
            )
            if created:
                charges_made += 1
            else:
                # Keep amount stable on re-run unless unpaid.
                if charge.amount_paid == 0:
                    charge.amount = amount
                    charge.due_date = due
                    charge.status = FeeCharge.Status.OPEN
                    charge.save()

            # One demo payment per student (half the charge, or 1000 minimum slice).
            pay_amount = (amount / Decimal("2")).quantize(Decimal("0.01"))
            if pay_amount < Decimal("500.00"):
                pay_amount = min(amount, Decimal("500.00"))

            already = Payment.objects.filter(
                student_id=student_id,
                reference=f"BKC-DEMO-{assess}",
            ).exists()
            if not already:
                allocate_payment_to_charges(
                    student_id=student_id,
                    amount=pay_amount,
                    method=Payment.Method.MPESA if i % 2 == 0 else Payment.Method.CASH,
                    reference=f"BKC-DEMO-{assess}",
                    notes=f"Demo payment for Best Kenya College learner {assess}",
                )
                # Stamp received_at for variety
                Payment.objects.filter(reference=f"BKC-DEMO-{assess}").update(
                    received_at=timezone.now() - timedelta(days=i)
                )
                payments_made += 1

        self.stdout.write(self.style.SUCCESS("Fee demo seed complete."))
        self.stdout.write(f"  Categories: {len(categories)}")
        self.stdout.write(f"  New charges: {charges_made}")
        self.stdout.write(f"  New payments: {payments_made}")
        self.stdout.write(
            "  Students: ASM1001–ASM1010 linked via admissions_student.id"
        )
