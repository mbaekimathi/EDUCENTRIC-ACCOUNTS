"""
Seed demo fee charges and payments for Best Kenya College learners.

Requires ADMINISTRATION seed first (students with assessment ASM1001–ASM1010).

  python manage.py seed_demo_fees
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from apps.billing.models import FeeCategory, FeeCharge, Payment, allocate_payment_to_charges


ASSESSMENT_NUMBERS = [f"ASM{1001 + i}" for i in range(10)]

# College fee heads (not CBC lunch/uniform school fees).
CATEGORY_DEFAULTS = [
    ("TUIT", "Tuition", "Semester tuition fees"),
    ("REG", "Registration", "Admission and semester registration"),
    ("EXAM", "Examination", "Internal CATs and final examinations"),
    ("LIB", "Library", "Library and e-resources access"),
    ("ICT", "ICT Levy", "Computer lab and internet access"),
    ("ATT", "Industrial Attachment", "Attachment / practicum administration"),
    ("ID", "Student ID", "College identity card"),
    ("MED", "Medical", "Student medical / first-aid cover"),
    ("SRC", "Student Welfare", "SRC and co-curricular activities"),
    ("DEV", "Development", "Infrastructure and development levy"),
]

CHARGE_AMOUNTS = [
    Decimal("45000.00"),
    Decimal("5000.00"),
    Decimal("3500.00"),
    Decimal("2000.00"),
    Decimal("2500.00"),
    Decimal("4000.00"),
    Decimal("1000.00"),
    Decimal("1500.00"),
    Decimal("1200.00"),
    Decimal("3000.00"),
]


def _lookup_student_ids_by_assessment(numbers: list[str]) -> dict[str, int]:
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
        "Seed 10 college fee categories, charges and payments "
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
            default="Semester 1",
            help="Semester label on charges (default: Semester 1)",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        year = options["academic_year"]
        term = options["term"]

        self.stdout.write("Seeding Best Kenya College fee demo data…")

        # Soft-deactivate schoolkid fee heads from earlier demos.
        FeeCategory.objects.filter(
            code__in=["LUNC", "UNIF", "TRAN", "BOOK", "ACTV", "LAB", "OTHR"]
        ).update(is_active=False)

        categories = []
        for code, name, description in CATEGORY_DEFAULTS:
            cat, made = FeeCategory.objects.get_or_create(
                code=code,
                defaults={"name": name, "description": description, "is_active": True},
            )
            if not made:
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
                    "notes": f"Demo college charge for {assess}",
                },
            )
            if created:
                charges_made += 1
            elif charge.amount_paid == 0:
                charge.amount = amount
                charge.due_date = due
                charge.status = FeeCharge.Status.OPEN
                charge.save()

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
                Payment.objects.filter(reference=f"BKC-DEMO-{assess}").update(
                    received_at=timezone.now() - timedelta(days=i)
                )
                payments_made += 1

        self.stdout.write(self.style.SUCCESS("College fee demo seed complete."))
        self.stdout.write(f"  Categories: {len(categories)}")
        self.stdout.write(f"  New charges: {charges_made}")
        self.stdout.write(f"  New payments: {payments_made}")
