from pathlib import Path

from django.core.management.base import BaseCommand

from apps.billing.models import FeeCategory
from apps.directory.models import Employee


class Command(BaseCommand):
    help = "Seed fee categories and show Accounts portal login status (idempotent)."

    def handle(self, *args, **options):
        defaults = [
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
        for code, name, description in defaults:
            _, made = FeeCategory.objects.get_or_create(
                code=code,
                defaults={"name": name, "description": description, "is_active": True},
            )
            if made:
                self.stdout.write(self.style.SUCCESS(f"Fee category {code} created"))

        portal_staff = Employee.objects.filter(
            role__in=Employee.PORTAL_ROLES,
            approval_status="APPROVED",
            is_suspended=False,
            is_active=True,
        ).order_by("role", "employee_code")
        count = portal_staff.count()
        self.stdout.write(self.style.SUCCESS("Bootstrap complete."))
        self.stdout.write(
            "Login uses Administration employees_employee "
            "(Accountant / Store Manager / IT Support only)."
        )
        self.stdout.write(f"Eligible employees: {count}")
        for emp in portal_staff[:10]:
            self.stdout.write(
                f"  {emp.employee_code} · {emp.role} · {emp.display_name}"
            )
        root = Path(__file__).resolve().parents[4]
        self.stdout.write(f"Project: {root}")
