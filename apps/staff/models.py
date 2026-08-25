from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.core.validators import RegexValidator
from django.db import models


class AccountsUserManager(BaseUserManager):
    def create_user(self, staff_code, email=None, password=None, **extra_fields):
        if not staff_code:
            raise ValueError("Staff code is required.")
        email = self.normalize_email(email)
        user = self.model(staff_code=staff_code, email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, staff_code, email=None, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)
        extra_fields.setdefault("role", AccountsUser.Role.ACCOUNTANT)
        return self.create_user(staff_code, email, password, **extra_fields)


class AccountsUser(AbstractUser):
    """Local session user mirrored from ADMINISTRATION employees on login."""

    class Role(models.TextChoices):
        ACCOUNTANT = "ACCOUNTANT", "Accountant"
        STORE_MANAGER = "STORE_MANAGER", "Store Manager"

    PORTAL_ROLES = frozenset({Role.ACCOUNTANT, Role.STORE_MANAGER})

    username = None
    staff_code = models.CharField(
        max_length=6,
        unique=True,
        validators=[RegexValidator(r"^\d{6}$", "Enter exactly six digits.")],
        help_text="Matches employees_employee.employee_code.",
    )
    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.ACCOUNTANT,
    )
    phone_number = models.CharField(max_length=24, blank=True)
    employee_id = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        unique=True,
        help_text="employees_employee.id from ADMINISTRATION",
    )

    USERNAME_FIELD = "staff_code"
    REQUIRED_FIELDS = ["email"]

    objects = AccountsUserManager()

    class Meta:
        db_table = "accounts_user"
        ordering = ["staff_code"]
        verbose_name = "accounts user"
        verbose_name_plural = "accounts users"

    def __str__(self):
        return f"{self.get_full_name() or self.staff_code} ({self.get_role_display()})"

    @property
    def can_access_portal(self):
        return self.is_active and self.role in {"ACCOUNTANT", "STORE_MANAGER"}
