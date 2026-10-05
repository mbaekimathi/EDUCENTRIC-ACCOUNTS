"""
Seed demo fee charges, payments, and (optionally) full finance showcase.

Requires ADMINISTRATION seed first (students ASM1001–ASM1010).

  python manage.py seed_demo_fees
  python manage.py seed_demo_fees --full --confirm-demo
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from apps.billing.models import (
    AccountTopUp,
    AccountWithdraw,
    FeeCategory,
    FeeCharge,
    FinancialTerm,
    FinancialYear,
    Payment,
    SchoolAccount,
    StoreItem,
    StoreStockMovement,
    StoreSupplier,
    allocate_payment_to_charges,
    ensure_store_lookups,
)


ASSESSMENT_NUMBERS = [f"ASM{1001 + i}" for i in range(10)]

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
        "Seed college fee categories/charges/payments. "
        "Use --full --confirm-demo for accounts, expenses, and store demo rows."
    )

    def add_arguments(self, parser):
        parser.add_argument("--academic-year", default="2026")
        parser.add_argument("--term", default="Semester 1")
        parser.add_argument(
            "--full",
            action="store_true",
            help="Also seed financial year, school accounts, top-ups, withdrawals, store.",
        )
        parser.add_argument(
            "--confirm-demo",
            action="store_true",
            help="Required with --full. Confirms demo DB (not a live school).",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        if options["full"] and not options["confirm_demo"]:
            raise CommandError(
                "Refusing --full without --confirm-demo.\n"
                "Only use on demo databases, never on a live school.\n"
                "Example: python manage.py seed_demo_fees --full --confirm-demo"
            )

        year = options["academic_year"]
        term = options["term"]
        self.stdout.write("Seeding Best Kenya College fee demo data…")

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

            # Extra open charge so some ledgers show unpaid balances.
            extra_title = f"Hostel / Facility levy — Best Kenya College {term} {year}"
            FeeCharge.objects.get_or_create(
                student_id=student_id,
                category=categories[9],  # DEV
                academic_year=year,
                term=term,
                title=extra_title,
                defaults={
                    "amount": Decimal("8000.00"),
                    "amount_paid": Decimal("0.00"),
                    "status": FeeCharge.Status.OPEN,
                    "due_date": due,
                    "notes": "Demo unpaid balance",
                },
            )

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

        if options["full"]:
            self._seed_full_finance()

    def _seed_full_finance(self):
        self.stdout.write("Seeding full finance showcase (accounts / expenses / store)…")

        fy, _ = FinancialYear.objects.get_or_create(
            name="2026/2026",
            defaults={
                "start_date": date(2026, 1, 1),
                "end_date": date(2026, 12, 31),
                "is_current": True,
            },
        )
        fy.is_current = True
        fy.save()
        FinancialTerm.objects.get_or_create(
            financial_year=fy,
            name="Semester 1",
            defaults={
                "start_date": date(2026, 1, 5),
                "end_date": date(2026, 4, 30),
                "is_current": True,
            },
        )

        fees_account, _ = SchoolAccount.objects.get_or_create(
            name="BKC Student Fees Account",
            defaults={
                "category": SchoolAccount.Category.STUDENT_FEES,
                "description": "Main tuition and levy collection account",
                "payment_modes": ["CASH", "MPESA", "BANK"],
                "is_active": True,
            },
        )
        petty, _ = SchoolAccount.objects.get_or_create(
            name="BKC Petty Cashbook",
            defaults={
                "category": SchoolAccount.Category.PETTY_CASHBOOK,
                "description": "Day-to-day college operations",
                "payment_modes": ["CASH", "MPESA"],
                "is_active": True,
            },
        )
        ops, _ = SchoolAccount.objects.get_or_create(
            name="BKC Operations Account",
            defaults={
                "category": SchoolAccount.Category.OPERATIONS,
                "description": "Utilities, suppliers, and admin spend",
                "payment_modes": ["CASH", "MPESA", "BANK", "CHEQUE"],
                "is_active": True,
            },
        )

        topups = [
            (fees_account, Decimal("2500000.00"), AccountTopUp.Method.CHEQUE, "BKC-TOP-FEES-001", "Opening fees float"),
            (petty, Decimal("150000.00"), AccountTopUp.Method.CASH, "BKC-TOP-PETTY-001", "Petty cash float"),
            (ops, Decimal("800000.00"), AccountTopUp.Method.MANUAL_MPESA, "BKC-TOP-OPS-001", "Operations top-up"),
            (fees_account, Decimal("450000.00"), AccountTopUp.Method.MANUAL_MPESA, "BKC-TOP-FEES-002", "M-Pesa collections batch"),
            (ops, Decimal("120000.00"), AccountTopUp.Method.CHEQUE, "BKC-TOP-OPS-002", "Sponsor cheque"),
        ]
        for account, amount, method, ref, desc in topups:
            AccountTopUp.objects.get_or_create(
                reference_number=ref,
                defaults={
                    "account": account,
                    "amount": amount,
                    "method": method,
                    "description": desc,
                    "reference_code": ref.replace("TOP", "TCODE")[:40],
                    "status": AccountTopUp.Status.APPROVED,
                },
            )

        # One pending top-up for approvals UI
        AccountTopUp.objects.get_or_create(
            reference_number="BKC-TOP-PENDING-001",
            defaults={
                "account": fees_account,
                "amount": Decimal("75000.00"),
                "method": AccountTopUp.Method.MANUAL_MPESA,
                "description": "Pending M-Pesa confirmation",
                "reference_code": "BKC-TCODE-PENDING-001",
                "status": AccountTopUp.Status.PENDING,
            },
        )

        expenses = [
            (petty, Decimal("12500.00"), "Office stationery restock", "BKC-WDR-001", "Nairobi Stationery Ltd"),
            (petty, Decimal("8500.00"), "Staff tea and hospitality", "BKC-WDR-002", "College Catering"),
            (ops, Decimal("45000.00"), "Internet and ICT maintenance", "BKC-WDR-003", "Safaricom Business"),
            (ops, Decimal("62000.00"), "Electricity bill — January", "BKC-WDR-004", "Kenya Power"),
            (ops, Decimal("28000.00"), "Water and sanitation", "BKC-WDR-005", "Nairobi Water"),
            (ops, Decimal("95000.00"), "Lab consumables", "BKC-WDR-006", "LabEquip Kenya"),
            (petty, Decimal("15000.00"), "Transport reclaim — attachment visits", "BKC-WDR-007", "Fleet Desk"),
            (ops, Decimal("35000.00"), "Printer toner and paper", "BKC-WDR-008", "OfficeMart"),
            (ops, Decimal("110000.00"), "Classroom furniture repair", "BKC-WDR-009", "WoodWorks Ltd"),
            (ops, Decimal("22000.00"), "Security patrol overtime", "BKC-WDR-010", "G4S Campus"),
        ]
        for account, amount, desc, ref, payee in expenses:
            AccountWithdraw.objects.get_or_create(
                reference_number=ref,
                defaults={
                    "account": account,
                    "amount": amount,
                    "method": AccountWithdraw.Method.MPESA
                    if "M-Pesa" in desc or "Internet" in desc
                    else AccountWithdraw.Method.CASH,
                    "payee": payee,
                    "description": desc,
                    "reference_code": ref.replace("WDR", "WCODE")[:40],
                    "status": AccountWithdraw.Status.APPROVED,
                },
            )

        AccountWithdraw.objects.get_or_create(
            reference_number="BKC-WDR-PENDING-001",
            defaults={
                "account": ops,
                "amount": Decimal("40000.00"),
                "method": AccountWithdraw.Method.BANK,
                "payee": "Pending Supplier Co",
                "description": "Pending approval — projector hire",
                "reference_code": "BKC-WCODE-PENDING-001",
                "status": AccountWithdraw.Status.PENDING,
            },
        )

        ensure_store_lookups()
        from apps.billing.models import StoreDepartmentStation, StoreExpenseCategory

        category = StoreExpenseCategory.objects.order_by("sort_order", "name").first()
        station = StoreDepartmentStation.objects.order_by("sort_order", "name").first()
        supplier, _ = StoreSupplier.objects.get_or_create(
            phone_number="+254700111222",
            defaults={"name": "BKC Campus Suppliers Ltd", "is_active": True},
        )

        items_spec = [
            ("Ream of A4 paper", StoreItem.Measure.REAM, "BKC-ITEM-PAPER"),
            ("Whiteboard marker set", StoreItem.Measure.SET, "BKC-ITEM-MARKER"),
            ("Laptop lock cable", StoreItem.Measure.PIECE, "BKC-ITEM-LOCK"),
            ("First-aid kit", StoreItem.Measure.BOX, "BKC-ITEM-FAID"),
            ("Projector HDMI cable", StoreItem.Measure.PIECE, "BKC-ITEM-HDMI"),
            ("Cleaning detergent 5L", StoreItem.Measure.L, "BKC-ITEM-CLEAN"),
            ("Lab gloves (box)", StoreItem.Measure.BOX, "BKC-ITEM-GLOVE"),
            ("Student ID PVC cards", StoreItem.Measure.PACKET, "BKC-ITEM-ID"),
            ("Toner cartridge", StoreItem.Measure.PIECE, "BKC-ITEM-TONER"),
            ("Extension cable 5m", StoreItem.Measure.PIECE, "BKC-ITEM-EXT"),
        ]
        for name, measure, ref in items_spec:
            if category is None or station is None:
                break
            item, _ = StoreItem.objects.get_or_create(
                reference_code=ref,
                defaults={
                    "expense_category": category,
                    "department_station": station,
                    "name": name,
                    "measure": measure,
                    "description": f"Demo store item — {name}",
                    "is_active": True,
                },
            )
            StoreStockMovement.objects.get_or_create(
                reference_code=f"SIN-{ref}",
                defaults={
                    "item": item,
                    "direction": StoreStockMovement.Direction.IN,
                    "quantity": Decimal("25.00"),
                    "supplier": supplier,
                    "payment_status": StoreStockMovement.PaymentStatus.PAID,
                    "invoice_amount": Decimal("15000.00"),
                    "amount_paid": Decimal("15000.00"),
                },
            )

        self.stdout.write(self.style.SUCCESS("Full finance showcase seeded."))
        self.stdout.write("  Financial year 2026 + Semester 1")
        self.stdout.write("  School accounts: fees / petty / operations")
        self.stdout.write("  Top-ups + withdrawals (incl. pending)")
        self.stdout.write("  Store catalogue + stock-in movements")
