"""
Read-only mirrors of ADMINISTRATION tables.

managed = False — ACCOUNTS never creates or alters these tables.
"""

from django.db import models


class ParentGuardian(models.Model):
    password = models.CharField(max_length=128, blank=True)
    is_active = models.BooleanField(default=False)
    full_name = models.CharField(max_length=200)
    relationship_to_student = models.CharField(max_length=80)
    phone_number = models.CharField(max_length=24, unique=True)
    email = models.EmailField(blank=True)
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "admissions_parentguardian"
        ordering = ["full_name"]

    def __str__(self):
        return self.full_name


class Student(models.Model):
    class AcademicLevel(models.TextChoices):
        PRE_PRIMARY_1 = "PRE_PRIMARY_1", "Pre-Primary 1"
        PRE_PRIMARY_2 = "PRE_PRIMARY_2", "Pre-Primary 2"
        GRADE_1 = "GRADE_1", "Grade 1"
        GRADE_2 = "GRADE_2", "Grade 2"
        GRADE_3 = "GRADE_3", "Grade 3"
        GRADE_4 = "GRADE_4", "Grade 4"
        GRADE_5 = "GRADE_5", "Grade 5"
        GRADE_6 = "GRADE_6", "Grade 6"
        GRADE_7 = "GRADE_7", "Grade 7"
        GRADE_8 = "GRADE_8", "Grade 8"
        GRADE_9 = "GRADE_9", "Grade 9"
        FORM_1 = "FORM_1", "Form 1"
        FORM_2 = "FORM_2", "Form 2"
        FORM_3 = "FORM_3", "Form 3"
        FORM_4 = "FORM_4", "Form 4"
        OTHER = "OTHER", "Other"

    password = models.CharField(max_length=128, blank=True)
    is_active = models.BooleanField(default=False)
    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150)
    date_of_birth = models.DateField()
    gender = models.CharField(max_length=10)
    academic_level = models.CharField(max_length=20, choices=AcademicLevel.choices)
    admission_number = models.CharField(max_length=40, unique=True, null=True, blank=True)
    class_group = models.CharField(max_length=50, blank=True)
    assessment_number = models.CharField(max_length=50, unique=True)
    previous_school = models.CharField(max_length=200, blank=True)
    profile_image = models.CharField(max_length=100, blank=True)
    sponsorship_category = models.CharField(max_length=20)
    sponsor_details = models.TextField(blank=True)
    parent_guardian = models.ForeignKey(
        ParentGuardian,
        on_delete=models.DO_NOTHING,
        related_name="students",
        db_constraint=False,
    )
    home_address = models.TextField(blank=True)
    medical_notes = models.TextField(blank=True)
    special_needs = models.TextField(blank=True)
    emergency_contact = models.CharField(max_length=200, blank=True)
    is_suspended = models.BooleanField(default=False)
    admitted_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "admissions_student"
        ordering = ["last_name", "first_name"]

    @property
    def display_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def __str__(self):
        return f"{self.admission_number or self.assessment_number} — {self.display_name}"


class SchoolProfile(models.Model):
    official_name = models.CharField(max_length=255)
    display_name = models.CharField(max_length=120)
    primary_color = models.CharField(max_length=7, blank=True)
    motto = models.CharField(max_length=255, blank=True)
    school_logo = models.CharField(max_length=100, blank=True)
    main_phone = models.CharField(max_length=24, blank=True)
    general_email = models.EmailField(blank=True)
    physical_address = models.TextField(blank=True)

    class Meta:
        managed = False
        db_table = "employees_schoolprofile"

    def __str__(self):
        return self.display_name or self.official_name


class Employee(models.Model):
    """Read-only mirror of ADMINISTRATION employees — used for Accounts portal login."""

    PORTAL_ROLES = frozenset({"ACCOUNTANT", "STORE_MANAGER", "IT_SUPPORT"})

    password = models.CharField(max_length=128)
    last_login = models.DateTimeField(null=True, blank=True)
    is_superuser = models.BooleanField(default=False)
    is_staff = models.BooleanField(default=False)
    is_active = models.BooleanField(default=False)
    date_joined = models.DateTimeField()
    employee_code = models.CharField(max_length=6, unique=True)
    title = models.CharField(max_length=4, blank=True)
    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150)
    email = models.EmailField()
    phone_number = models.CharField(max_length=24, blank=True)
    profile_image = models.CharField(max_length=100, blank=True)
    approval_status = models.CharField(max_length=20)
    role = models.CharField(max_length=32)
    is_suspended = models.BooleanField(default=False)
    employment_number = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "employees_employee"
        ordering = ["last_name", "first_name"]

    def __str__(self):
        return f"{self.employee_code} — {self.display_name}"

    @property
    def display_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def check_password(self, raw_password: str) -> bool:
        from django.contrib.auth.hashers import check_password

        return check_password(raw_password, self.password)

    @property
    def can_access_accounts_portal(self) -> bool:
        return (
            self.role in self.PORTAL_ROLES
            and self.approval_status == "APPROVED"
            and not self.is_suspended
            and self.is_active
        )


class AcademicLevel(models.Model):
    """Read-only mirror of ADMINISTRATION curriculum academic levels."""

    name = models.CharField(max_length=120)
    code = models.CharField(max_length=40)
    description = models.TextField(blank=True)
    order = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=10, blank=True)
    category = models.CharField(max_length=120, blank=True)

    class Meta:
        managed = False
        db_table = "curriculum_academiclevel"
        ordering = ["order", "name"]

    def __str__(self):
        return self.name
