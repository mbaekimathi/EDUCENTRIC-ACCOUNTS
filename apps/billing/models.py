"""
Finance tables owned by ACCOUNTS.

Student links use integer IDs (no DB-level FK to admissions_student) so
ADMINISTRATION can evolve its schema without migration conflicts here.
"""

from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Sum
from django.utils import timezone


class FeeCategory(models.Model):
    """e.g. Tuition, Transport, Lunch, Exam fee."""

    name = models.CharField(max_length=120, unique=True)
    code = models.CharField(max_length=40, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "accounts_fee_category"
        ordering = ["name"]
        verbose_name_plural = "fee categories"

    def __str__(self):
        return f"{self.code} — {self.name}"


class FeeCharge(models.Model):
    """A charge posted to a learner's ledger (invoice line)."""

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        PARTIAL = "PARTIAL", "Partially paid"
        PAID = "PAID", "Paid"
        WAIVED = "WAIVED", "Waived"
        CANCELLED = "CANCELLED", "Cancelled"

    student_id = models.PositiveBigIntegerField(
        db_index=True,
        help_text="admissions_student.id (no DB FK — shared with ADMINISTRATION)",
    )
    category = models.ForeignKey(
        FeeCategory,
        on_delete=models.PROTECT,
        related_name="charges",
    )
    title = models.CharField(max_length=200)
    academic_year = models.CharField(max_length=20, blank=True)
    term = models.CharField(max_length=40, blank=True)
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    due_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    fee_structure = models.ForeignKey(
        "FeeStructure",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="charges",
    )
    structure_line = models.ForeignKey(
        "FeeStructureLine",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="charges",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="fee_charges_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "accounts_fee_charge"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["student_id", "status"]),
            models.Index(fields=["academic_year", "term"]),
            models.Index(fields=["student_id", "structure_line"]),
        ]

    def __str__(self):
        return f"{self.title} ({self.amount}) — student #{self.student_id}"

    @property
    def balance(self):
        return self.amount - self.amount_paid

    def refresh_status(self, save=True):
        if self.status == self.Status.WAIVED or self.status == self.Status.CANCELLED:
            return
        if self.amount_paid <= 0:
            self.status = self.Status.OPEN
        elif self.amount_paid >= self.amount:
            self.status = self.Status.PAID
            self.amount_paid = self.amount
        else:
            self.status = self.Status.PARTIAL
        if save:
            self.save(update_fields=["status", "amount_paid", "updated_at"])


class Payment(models.Model):
    """Money received against one or more charges (MVP: one charge)."""

    class Method(models.TextChoices):
        CASH = "CASH", "Cash"
        MPESA = "MPESA", "M-Pesa"
        BANK = "BANK", "Bank transfer"
        CHEQUE = "CHEQUE", "Cheque"
        BARTER = "BARTER", "Barter trade"
        OTHER = "OTHER", "Other"

    student_id = models.PositiveBigIntegerField(db_index=True)
    charge = models.ForeignKey(
        FeeCharge,
        on_delete=models.PROTECT,
        related_name="payments",
        null=True,
        blank=True,
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    method = models.CharField(max_length=20, choices=Method.choices, default=Method.CASH)
    reference = models.CharField(max_length=120, blank=True)
    received_at = models.DateTimeField(default=timezone.now)
    notes = models.TextField(blank=True)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payments_received",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "accounts_payment"
        ordering = ["-received_at"]

    def __str__(self):
        return f"{self.amount} via {self.method} — student #{self.student_id}"


def student_balance(student_id: int) -> Decimal:
    """Open balance for a learner across all non-cancelled/waived charges."""
    agg = (
        FeeCharge.objects.filter(student_id=student_id)
        .exclude(status__in=[FeeCharge.Status.CANCELLED, FeeCharge.Status.WAIVED])
        .aggregate(
            charged=Sum("amount"),
            paid=Sum("amount_paid"),
        )
    )
    charged = agg["charged"] or Decimal("0.00")
    paid = agg["paid"] or Decimal("0.00")
    return charged - paid


def allocate_payment_to_charges(
    student_id: int,
    amount: Decimal,
    method: str,
    reference: str = "",
    received_by=None,
    notes: str = "",
) -> dict:
    """
    Apply a payment to open fee charges (oldest first).
    Creates one Payment row per charge slice; leftover becomes an unallocated Payment.
    """
    from django.db import transaction

    if amount <= 0:
        raise ValueError("Payment amount must be greater than zero.")

    remaining = amount
    payments = []
    with transaction.atomic():
        charges = list(
            FeeCharge.objects.select_for_update()
            .filter(student_id=student_id)
            .exclude(
                status__in=[
                    FeeCharge.Status.CANCELLED,
                    FeeCharge.Status.WAIVED,
                    FeeCharge.Status.PAID,
                ]
            )
            .order_by("created_at", "id")
        )
        for charge in charges:
            if remaining <= 0:
                break
            due = charge.amount - charge.amount_paid
            if due <= 0:
                continue
            pay = due if due <= remaining else remaining
            charge.amount_paid = charge.amount_paid + pay
            charge.refresh_status(save=True)
            payment = Payment.objects.create(
                student_id=student_id,
                charge=charge,
                amount=pay,
                method=method,
                reference=reference,
                notes=notes,
                received_by=received_by,
            )
            payments.append(payment)
            remaining -= pay

        unallocated = Decimal("0.00")
        if remaining > 0:
            unallocated = remaining
            payments.append(
                Payment.objects.create(
                    student_id=student_id,
                    charge=None,
                    amount=remaining,
                    method=method,
                    reference=reference,
                    notes=(notes + " · unallocated credit").strip(" ·"),
                    received_by=received_by,
                )
            )

    return {
        "allocated": amount - unallocated,
        "unallocated": unallocated,
        "payments": payments,
        "payment_count": len(payments),
    }


class SchoolAccount(models.Model):
    """Registered school finance account (owned by ACCOUNTS)."""

    class Category(models.TextChoices):
        STUDENT_FEES = "STUDENT_FEES", "Student fees"
        POCKET_MONEY = "POCKET_MONEY", "Pocket money"
        PETTY_CASHBOOK = "PETTY_CASHBOOK", "Petty cashbook"
        CAPITATION = "CAPITATION", "Capitation grant"
        OPERATIONS = "OPERATIONS", "Operations"
        CAPITAL = "CAPITAL", "Capital"
        OTHER = "OTHER", "Other"

    class PaymentMode(models.TextChoices):
        CASH = "CASH", "Cash"
        MPESA = "MPESA", "M-Pesa"
        BANK = "BANK", "Bank transfer"
        CHEQUE = "CHEQUE", "Cheque"
        BARTER = "BARTER", "Barter trade"
        OTHER = "OTHER", "Other"

    class VoteFundAllocation(models.TextChoices):
        EQUAL = "EQUAL", "Equally among votes"
        PRIORITY_ORDER = "PRIORITY_ORDER", "By allocation order (priority)"
        DISABLED = "DISABLED", "Disable votes"

    category = models.CharField(max_length=32, choices=Category.choices)
    custom_category = models.CharField(max_length=120, blank=True)
    name = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    payment_modes = models.JSONField(default=list)
    academic_level_ids = models.JSONField(default=list, blank=True)
    vote_fund_allocation = models.CharField(
        max_length=32,
        choices=VoteFundAllocation.choices,
        default=VoteFundAllocation.PRIORITY_ORDER,
        help_text="How incoming funds are allocated across votes on this account.",
    )
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="school_accounts_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "accounts_school_account"
        ordering = ["category", "name"]

    def __str__(self):
        return f"{self.name} ({self.category_label})"

    @property
    def category_label(self):
        if self.category == self.Category.OTHER and self.custom_category:
            return self.custom_category
        return self.get_category_display()

    @property
    def payment_mode_labels(self):
        labels = dict(self.PaymentMode.choices)
        return [labels.get(code, code) for code in (self.payment_modes or [])]

    @property
    def votes_enabled(self):
        return self.vote_fund_allocation != self.VoteFundAllocation.DISABLED


def generate_topup_reference_code() -> str:
    from django.utils import timezone
    import secrets

    stamp = timezone.now().strftime("%Y%m%d%H%M")
    return f"TOP-{stamp}-{secrets.token_hex(3).upper()}"


class AccountTopUp(models.Model):
    """Funds credited to a registered school account."""

    class Method(models.TextChoices):
        STK_PUSH = "STK_PUSH", "STK Push (M-Pesa)"
        MANUAL_MPESA = "MANUAL_MPESA", "Manual M-Pesa"
        CASH = "CASH", "Cash"
        CHEQUE = "CHEQUE", "Cheque"
        TRADE = "TRADE", "Trade"

    class Status(models.TextChoices):
        APPROVED = "APPROVED", "Approved"
        PENDING = "PENDING", "Pending"
        FAILED = "FAILED", "Failed"

    account = models.ForeignKey(
        SchoolAccount,
        on_delete=models.PROTECT,
        related_name="top_ups",
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    method = models.CharField(max_length=20, choices=Method.choices)
    description = models.CharField(max_length=255, blank=True)
    reference_number = models.CharField(max_length=120)
    reference_code = models.CharField(max_length=40, unique=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.APPROVED,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="account_top_ups_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "accounts_account_topup"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.reference_code} · {self.amount}"

    def save(self, *args, **kwargs):
        if not self.reference_code:
            for _ in range(8):
                candidate = generate_topup_reference_code()
                if not type(self).objects.filter(reference_code=candidate).exists():
                    self.reference_code = candidate
                    break
            if not self.reference_code:
                self.reference_code = generate_topup_reference_code()
        super().save(*args, **kwargs)


def generate_withdraw_reference_code() -> str:
    from django.utils import timezone
    import secrets

    stamp = timezone.now().strftime("%Y%m%d%H%M")
    return f"WDR-{stamp}-{secrets.token_hex(3).upper()}"


class AccountWithdraw(models.Model):
    """Funds withdrawn from a petty cashbook account."""

    class Method(models.TextChoices):
        CASH = "CASH", "Cash"
        MPESA = "MPESA", "M-Pesa"
        BANK = "BANK", "Bank transfer"
        CHEQUE = "CHEQUE", "Cheque"
        BARTER = "BARTER", "Barter trade"
        OTHER = "OTHER", "Other"

    class Status(models.TextChoices):
        APPROVED = "APPROVED", "Approved"
        PENDING = "PENDING", "Pending"
        CANCELLED = "CANCELLED", "Cancelled"

    account = models.ForeignKey(
        SchoolAccount,
        on_delete=models.PROTECT,
        related_name="withdrawals",
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    method = models.CharField(max_length=20, choices=Method.choices)
    payee = models.CharField(max_length=160, help_text="Person or party receiving the funds.")
    description = models.CharField(max_length=255, blank=True)
    reference_number = models.CharField(max_length=120)
    reference_code = models.CharField(max_length=40, unique=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.APPROVED,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="account_withdrawals_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "accounts_account_withdraw"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.reference_code} · {self.amount}"

    def save(self, *args, **kwargs):
        if not self.reference_code:
            for _ in range(8):
                candidate = generate_withdraw_reference_code()
                if not type(self).objects.filter(reference_code=candidate).exists():
                    self.reference_code = candidate
                    break
            if not self.reference_code:
                self.reference_code = generate_withdraw_reference_code()
        super().save(*args, **kwargs)


def generate_pocket_money_reference_code() -> str:
    import secrets

    stamp = timezone.now().strftime("%Y%m%d%H%M")
    return f"PM-{stamp}-{secrets.token_hex(3).upper()}"


class StudentPocketMoneyEntry(models.Model):
    """Per-learner pocket money credit or debit against a pocket money account."""

    class Direction(models.TextChoices):
        CREDIT = "CREDIT", "Top up"
        DEBIT = "DEBIT", "Withdraw"

    class Method(models.TextChoices):
        CASH = "CASH", "Cash"
        MANUAL_MPESA = "MANUAL_MPESA", "Manual M-Pesa"
        STK_PUSH = "STK_PUSH", "STK Push (M-Pesa)"
        CHEQUE = "CHEQUE", "Cheque"
        BANK = "BANK", "Bank transfer"
        OTHER = "OTHER", "Other"

    class Status(models.TextChoices):
        APPROVED = "APPROVED", "Approved"
        PENDING = "PENDING", "Pending"
        CANCELLED = "CANCELLED", "Cancelled"

    account = models.ForeignKey(
        SchoolAccount,
        on_delete=models.PROTECT,
        related_name="pocket_money_entries",
    )
    student_id = models.PositiveBigIntegerField(
        db_index=True,
        help_text="admissions_student.id (no DB FK — shared with ADMINISTRATION)",
    )
    direction = models.CharField(max_length=10, choices=Direction.choices)
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    method = models.CharField(max_length=20, choices=Method.choices)
    description = models.CharField(max_length=255, blank=True)
    reference_number = models.CharField(max_length=120)
    reference_code = models.CharField(max_length=40, unique=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.APPROVED,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pocket_money_entries_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "accounts_student_pocket_money_entry"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["account", "student_id"]),
        ]

    def __str__(self):
        return f"{self.reference_code} · {self.direction} · {self.amount}"

    def save(self, *args, **kwargs):
        if not self.reference_code:
            for _ in range(8):
                candidate = generate_pocket_money_reference_code()
                if not type(self).objects.filter(reference_code=candidate).exists():
                    self.reference_code = candidate
                    break
            if not self.reference_code:
                self.reference_code = generate_pocket_money_reference_code()
        super().save(*args, **kwargs)


def student_pocket_money_balance(account, student_id) -> Decimal:
    """Approved credits minus approved debits for one learner on a pocket money account."""
    credits = (
        StudentPocketMoneyEntry.objects.filter(
            account=account,
            student_id=student_id,
            direction=StudentPocketMoneyEntry.Direction.CREDIT,
            status=StudentPocketMoneyEntry.Status.APPROVED,
        ).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    debits = (
        StudentPocketMoneyEntry.objects.filter(
            account=account,
            student_id=student_id,
            direction=StudentPocketMoneyEntry.Direction.DEBIT,
            status=StudentPocketMoneyEntry.Status.APPROVED,
        ).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    return credits - debits


def generate_vote_reference_code() -> str:
    from django.utils import timezone
    import secrets

    stamp = timezone.now().strftime("%Y%m%d%H%M")
    return f"VOT-{stamp}-{secrets.token_hex(3).upper()}"


class AccountVote(models.Model):
    """Budget vote head registered against a school account."""

    class Status(models.TextChoices):
        APPROVED = "APPROVED", "Approved"
        PENDING = "PENDING", "Pending"
        SUSPENDED = "SUSPENDED", "Suspended"
        CANCELLED = "CANCELLED", "Cancelled"

    class AllocationMode(models.TextChoices):
        AMOUNT = "AMOUNT", "Fixed amount"
        PERCENTAGE = "PERCENTAGE", "Percentage of total topped up"

    account = models.ForeignKey(
        SchoolAccount,
        on_delete=models.PROTECT,
        related_name="votes",
    )
    name = models.CharField(max_length=160)
    code = models.CharField(max_length=40, blank=True)
    description = models.CharField(max_length=255, blank=True)
    allocation_mode = models.CharField(
        max_length=20,
        choices=AllocationMode.choices,
        default=AllocationMode.AMOUNT,
    )
    percentage = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("1"))],
        help_text="Share of the account’s total approved top-ups (whole number, 1–100).",
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Allocated amount. May be 0 until the account is topped up.",
    )
    allocation_order = models.PositiveIntegerField(
        default=1,
        help_text="Priority order for allocating funds on this account (1 = first).",
    )
    reference_code = models.CharField(max_length=40, unique=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.APPROVED,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="account_votes_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "accounts_account_vote"
        ordering = ["allocation_order", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "allocation_order"],
                name="uniq_account_vote_allocation_order",
            )
        ]

    def __str__(self):
        return f"{self.name} · {self.reference_code}"

    def save(self, *args, **kwargs):
        if not self.reference_code:
            for _ in range(8):
                candidate = generate_vote_reference_code()
                if not type(self).objects.filter(reference_code=candidate).exists():
                    self.reference_code = candidate
                    break
            if not self.reference_code:
                self.reference_code = generate_vote_reference_code()
        if not self.code:
            self.code = self.reference_code
        super().save(*args, **kwargs)


class DarajaSettings(models.Model):
    """Singleton Safaricom Daraja (M-Pesa) API credentials for sandbox and production."""

    class Environment(models.TextChoices):
        SANDBOX = "SANDBOX", "Sandbox"
        PRODUCTION = "PRODUCTION", "Production"

    active_environment = models.CharField(
        max_length=20,
        choices=Environment.choices,
        default=Environment.SANDBOX,
    )
    is_enabled = models.BooleanField(default=False)

    sandbox_consumer_key = models.CharField(max_length=255, blank=True)
    sandbox_consumer_secret = models.CharField(max_length=255, blank=True)
    sandbox_shortcode = models.CharField(max_length=20, blank=True)
    sandbox_passkey = models.CharField(max_length=255, blank=True)
    sandbox_callback_url = models.URLField(blank=True)

    production_consumer_key = models.CharField(max_length=255, blank=True)
    production_consumer_secret = models.CharField(max_length=255, blank=True)
    production_shortcode = models.CharField(max_length=20, blank=True)
    production_passkey = models.CharField(max_length=255, blank=True)
    production_callback_url = models.URLField(blank=True)

    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="daraja_settings_updates",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "accounts_daraja_settings"
        verbose_name = "Daraja settings"
        verbose_name_plural = "Daraja settings"

    def __str__(self):
        return f"Daraja ({self.get_active_environment_display()})"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def api_base_url(self) -> str:
        if self.active_environment == self.Environment.PRODUCTION:
            return "https://api.safaricom.co.ke"
        return "https://sandbox.safaricom.co.ke"

    def active_config(self) -> dict:
        if self.active_environment == self.Environment.PRODUCTION:
            return {
                "environment": self.Environment.PRODUCTION,
                "consumer_key": (self.production_consumer_key or "").strip(),
                "consumer_secret": (self.production_consumer_secret or "").strip(),
                "shortcode": (self.production_shortcode or "").strip(),
                "passkey": (self.production_passkey or "").strip(),
                "callback_url": (self.production_callback_url or "").strip(),
            }
        return {
            "environment": self.Environment.SANDBOX,
            "consumer_key": (self.sandbox_consumer_key or "").strip(),
            "consumer_secret": (self.sandbox_consumer_secret or "").strip(),
            "shortcode": (self.sandbox_shortcode or "").strip(),
            "passkey": (self.sandbox_passkey or "").strip(),
            "callback_url": (self.sandbox_callback_url or "").strip(),
        }

    def stk_missing_fields(self) -> list:
        if not self.is_enabled:
            return ["M-Pesa is disabled"]
        cfg = self.active_config()
        missing = []
        if not cfg["consumer_key"]:
            missing.append("consumer key")
        if not cfg["consumer_secret"]:
            missing.append("consumer secret")
        if not cfg["shortcode"]:
            missing.append("business shortcode")
        if not cfg["passkey"]:
            missing.append("Lipa Na M-Pesa passkey")
        if not cfg["callback_url"]:
            missing.append("callback URL")
        elif "/mpesa/callback" not in cfg["callback_url"].lower():
            missing.append(
                "callback URL path (must end with /accounts-dashboard/mpesa/callback/)"
            )
        return missing

    def stk_ready(self) -> bool:
        return not self.stk_missing_fields()


class MpesaCallbackLog(models.Model):
    """Raw Daraja / STK Push callback payloads for audit and local testing."""

    class Status(models.TextChoices):
        RECEIVED = "RECEIVED", "Received"
        SUCCESS = "SUCCESS", "Success"
        FAILED = "FAILED", "Failed"
        IGNORED = "IGNORED", "Ignored"

    merchant_request_id = models.CharField(max_length=64, blank=True, db_index=True)
    checkout_request_id = models.CharField(max_length=64, blank=True, db_index=True)
    result_code = models.IntegerField(null=True, blank=True)
    result_desc = models.CharField(max_length=255, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    mpesa_receipt = models.CharField(max_length=64, blank=True, db_index=True)
    phone_number = models.CharField(max_length=20, blank=True)
    transaction_date = models.CharField(max_length=20, blank=True)
    account_reference = models.CharField(max_length=120, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RECEIVED)
    raw_payload = models.JSONField(default=dict)
    payment = models.ForeignKey(
        Payment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="mpesa_callbacks",
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "accounts_mpesa_callback_log"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.mpesa_receipt or self.checkout_request_id or self.pk} ({self.status})"


class StkPushRequest(models.Model):
    """Tracks an initiated Lipa Na M-Pesa STK Push for a learner fee payment."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        SUCCESS = "SUCCESS", "Success"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled"

    student_id = models.PositiveBigIntegerField(db_index=True)
    account = models.ForeignKey(
        SchoolAccount,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stk_push_requests",
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    phone_number = models.CharField(max_length=20)
    account_reference = models.CharField(max_length=120, blank=True)
    merchant_request_id = models.CharField(max_length=64, blank=True, db_index=True)
    checkout_request_id = models.CharField(max_length=64, blank=True, db_index=True)
    mpesa_receipt = models.CharField(max_length=64, blank=True, db_index=True)
    result_code = models.IntegerField(null=True, blank=True)
    result_desc = models.CharField(max_length=255, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stk_push_requests_created",
    )
    payment = models.ForeignKey(
        Payment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stk_push_requests",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "accounts_stk_push_request"
        ordering = ["-created_at"]

    def __str__(self):
        return f"STK {self.checkout_request_id or self.pk} · student #{self.student_id}"


class FinancialYear(models.Model):
    """School financial year window used for Accounts period reporting."""

    name = models.CharField(max_length=40, blank=True)
    start_date = models.DateField()
    end_date = models.DateField()
    is_current = models.BooleanField(
        default=False,
        help_text="Only one financial year should be marked current.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="financial_years_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "accounts_financial_year"
        ordering = ["-start_date"]

    def __str__(self):
        return self.display_name

    @property
    def display_name(self):
        if self.name:
            return self.name
        return f"{self.start_date:%Y}/{self.end_date:%Y}"

    def clean(self):
        from django.core.exceptions import ValidationError

        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValidationError({"end_date": "End date must be on or after the start date."})

    def save(self, *args, **kwargs):
        if not self.name and self.start_date and self.end_date:
            self.name = f"{self.start_date.year}/{self.end_date.year}"
        was_current = False
        if self.pk:
            was_current = type(self).objects.filter(pk=self.pk, is_current=True).exists()
        becoming_current = self.is_current
        super().save(*args, **kwargs)

        from .financial_year import apply_default_current_term, clear_current_terms

        if becoming_current:
            type(self).objects.exclude(pk=self.pk).filter(is_current=True).update(
                is_current=False
            )
            clear_current_terms(except_year=self)
            # Default current term to today’s range when this year newly becomes current,
            # or when the current year has no current term yet.
            if not was_current or not self.terms.filter(is_current=True).exists():
                apply_default_current_term(self, force=True)
        else:
            self.terms.filter(is_current=True).update(is_current=False)

    @classmethod
    def current(cls):
        return cls.objects.filter(is_current=True).first()


class FinancialTerm(models.Model):
    """Term period within a financial year."""

    financial_year = models.ForeignKey(
        FinancialYear,
        on_delete=models.CASCADE,
        related_name="terms",
    )
    name = models.CharField(max_length=80)
    start_date = models.DateField()
    end_date = models.DateField()
    is_current = models.BooleanField(
        default=False,
        help_text="Only valid on the current financial year; defaults to today’s term.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "accounts_financial_term"
        ordering = ["start_date", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["financial_year", "name"],
                name="uniq_financial_year_term_name",
            )
        ]

    def __str__(self):
        return f"{self.name} ({self.financial_year.display_name})"

    def clean(self):
        from django.core.exceptions import ValidationError

        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValidationError({"end_date": "Term end date must be on or after the start date."})
        year = self.financial_year
        if year and self.start_date and self.end_date:
            if self.start_date < year.start_date or self.end_date > year.end_date:
                raise ValidationError(
                    "Term dates must fall within the financial year start and end dates."
                )
        if self.is_current and year and not year.is_current:
            raise ValidationError(
                "Current term can only be set on the current financial year."
            )

    def save(self, *args, **kwargs):
        from .financial_year import clear_current_terms

        if self.is_current and self.financial_year_id:
            year = self.financial_year
            if not year.is_current:
                # Promote parent year without re-running default-term overwrite.
                type(year).objects.filter(pk=year.pk).update(is_current=True)
                type(year).objects.exclude(pk=year.pk).filter(is_current=True).update(
                    is_current=False
                )
                clear_current_terms(except_year=year)
                year.is_current = True
        super().save(*args, **kwargs)
        if self.is_current:
            type(self).objects.exclude(pk=self.pk).filter(is_current=True).update(
                is_current=False
            )

    @classmethod
    def current(cls):
        return (
            cls.objects.filter(is_current=True, financial_year__is_current=True)
            .select_related("financial_year")
            .first()
        )


def generate_fee_structure_reference_code() -> str:
    import secrets

    stamp = timezone.now().strftime("%Y%m%d%H%M")
    return f"FST-{stamp}-{secrets.token_hex(3).upper()}"


class FeeStructure(models.Model):
    """Session fee structure for a student-fees account (vote heads × levels)."""

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        DRAFT = "DRAFT", "Draft"
        ARCHIVED = "ARCHIVED", "Archived"

    account = models.ForeignKey(
        SchoolAccount,
        on_delete=models.PROTECT,
        related_name="fee_structures",
    )
    financial_year = models.ForeignKey(
        FinancialYear,
        on_delete=models.PROTECT,
        related_name="fee_structures",
    )
    financial_term = models.ForeignKey(
        FinancialTerm,
        on_delete=models.PROTECT,
        related_name="fee_structures",
    )
    name = models.CharField(max_length=160, blank=True)
    academic_level_ids = models.JSONField(
        default=list,
        blank=True,
        help_text="Curriculum academic level IDs this structure applies to.",
    )
    reference_code = models.CharField(max_length=40, unique=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    notes = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="fee_structures_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "accounts_fee_structure"
        ordering = ["-created_at"]

    def __str__(self):
        return self.display_name

    @property
    def display_name(self):
        if self.name:
            return self.name
        return f"{self.financial_term.name} · {self.financial_year.display_name}"

    def save(self, *args, **kwargs):
        if not self.reference_code:
            for _ in range(8):
                candidate = generate_fee_structure_reference_code()
                if not type(self).objects.filter(reference_code=candidate).exists():
                    self.reference_code = candidate
                    break
            if not self.reference_code:
                self.reference_code = generate_fee_structure_reference_code()
        if self.financial_term_id and self.financial_year_id:
            term = self.financial_term
            year = self.financial_year
            self.name = f"{term.name} · {year.display_name}"
        super().save(*args, **kwargs)

    @property
    def total_amount(self):
        return self.lines.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")


class FeeStructureLine(models.Model):
    """One vote-head amount within a session fee structure."""

    structure = models.ForeignKey(
        FeeStructure,
        on_delete=models.CASCADE,
        related_name="lines",
    )
    vote = models.ForeignKey(
        AccountVote,
        on_delete=models.PROTECT,
        related_name="fee_structure_lines",
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    class Meta:
        db_table = "accounts_fee_structure_line"
        ordering = ["vote__allocation_order", "vote__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["structure", "vote"],
                name="uniq_fee_structure_vote",
            )
        ]

    def __str__(self):
        return f"{self.vote.name}: {self.amount}"


STORE_EXPENSE_CATEGORY_DEFAULTS = (
    "Stationery",
    "Cleaning materials",
    "Foodstuffs",
    "Laboratory",
    "Sports and games",
    "Uniforms",
    "Maintenance and repairs",
    "Furniture and fittings",
    "Teaching materials",
    "Medical supplies",
    "Fuel and energy",
    "Other",
)

STORE_DEPARTMENT_STATION_DEFAULTS = (
    "Administration",
    "Boarding",
    "Kitchen",
    "Science laboratory",
    "Computer laboratory",
    "Library",
    "Sports",
    "Farm",
    "Workshop",
    "Clinic",
    "Store",
    "Accounts",
    "Other",
)


def generate_item_reference_code() -> str:
    import secrets

    from django.utils import timezone

    stamp = timezone.now().strftime("%Y%m%d")
    return f"ITM-{stamp}-{secrets.token_hex(3).upper()}"


def normalize_supplier_phone(value: str) -> str:
    """Keep digits and a leading + for supplier phone matching."""
    raw = (value or "").strip()
    if not raw:
        return ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if raw.startswith("+") and digits:
        return f"+{digits}"
    return digits


class StoreSupplier(models.Model):
    """Supplier used for store deliveries and payment follow-up."""

    name = models.CharField(max_length=160)
    phone_number = models.CharField(max_length=24, unique=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="store_suppliers_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "accounts_store_supplier"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.phone_number})"


class StoreExpenseCategory(models.Model):
    """Vote-style expense grouping for store items."""

    name = models.CharField(max_length=120, unique=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "accounts_store_expense_category"
        ordering = ["sort_order", "name"]
        verbose_name_plural = "store expense categories"

    def __str__(self):
        return self.name


class StoreDepartmentStation(models.Model):
    """Department or station that holds or uses store items."""

    name = models.CharField(max_length=120, unique=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "accounts_store_department_station"
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


class StoreItem(models.Model):
    """Catalogue item registered in the school store."""

    class Measure(models.TextChoices):
        PIECE = "PCS", "Piece"
        PAIR = "PAIR", "Pair"
        DOZEN = "DOZEN", "Dozen"
        PACKET = "PKT", "Packet"
        BOX = "BOX", "Box"
        CARTON = "CTN", "Carton"
        REAM = "REAM", "Ream"
        SET = "SET", "Set"
        KG = "KG", "Kilogram"
        G = "G", "Gram"
        L = "L", "Litre"
        ML = "ML", "Millilitre"
        M = "M", "Metre"
        ROLL = "ROLL", "Roll"
        TIN = "TIN", "Tin"
        BOTTLE = "BTL", "Bottle"
        BAG = "BAG", "Bag"

    expense_category = models.ForeignKey(
        StoreExpenseCategory,
        on_delete=models.PROTECT,
        related_name="items",
    )
    department_station = models.ForeignKey(
        StoreDepartmentStation,
        on_delete=models.PROTECT,
        related_name="items",
    )
    name = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    measure = models.CharField(max_length=12, choices=Measure.choices)
    image = models.ImageField(upload_to="store/items/%Y/%m/", blank=True)
    reference_code = models.CharField(max_length=40, unique=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="store_items_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "accounts_store_item"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["name", "department_station", "measure"],
                name="uniq_store_item_name_station_measure",
            )
        ]

    def __str__(self):
        return f"{self.reference_code} — {self.name}"

    def save(self, *args, **kwargs):
        if not self.reference_code:
            for _ in range(8):
                candidate = generate_item_reference_code()
                if not type(self).objects.filter(reference_code=candidate).exists():
                    self.reference_code = candidate
                    break
            if not self.reference_code:
                self.reference_code = generate_item_reference_code()
        super().save(*args, **kwargs)


def generate_stock_movement_reference_code(direction: str) -> str:
    import secrets

    prefix = "SIN" if direction == "IN" else "SOU"
    stamp = timezone.now().strftime("%Y%m%d")
    return f"{prefix}-{stamp}-{secrets.token_hex(3).upper()}"


class StoreStockMovement(models.Model):
    """Stock received into or issued from the school store."""

    class Direction(models.TextChoices):
        IN = "IN", "Stock in"
        OUT = "OUT", "Stock out"

    class PaymentStatus(models.TextChoices):
        UNPAID = "UNPAID", "Not paid"
        PENDING = "PENDING", "Pending payment"
        PARTIAL = "PARTIAL", "Partially paid"
        PAID = "PAID", "Paid"

    class OutReason(models.TextChoices):
        TRANSFER = "TRANSFER", "Transfer to department"
        WASTE = "WASTE", "Waste"
        RETURN = "RETURN", "Return to supplier"

    item = models.ForeignKey(
        StoreItem,
        on_delete=models.PROTECT,
        related_name="movements",
    )
    direction = models.CharField(max_length=8, choices=Direction.choices)
    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    supplier = models.ForeignKey(
        StoreSupplier,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_movements",
    )
    payment_status = models.CharField(
        max_length=20,
        choices=PaymentStatus.choices,
        blank=True,
    )
    out_reason = models.CharField(
        max_length=20,
        choices=OutReason.choices,
        blank=True,
    )
    destination_station = models.ForeignKey(
        "StoreDepartmentStation",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_transfers_received",
    )
    unit_buying_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Buying price per unit at stock in.",
    )
    invoice_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    amount_paid = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    notes = models.CharField(max_length=255, blank=True)
    reference_code = models.CharField(max_length=40, unique=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="store_stock_movements_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "accounts_store_stock_movement"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.reference_code} · {self.get_direction_display()}"

    @property
    def amount_outstanding(self):
        if self.invoice_amount is None:
            return None
        return max(self.invoice_amount - (self.amount_paid or Decimal("0.00")), Decimal("0.00"))

    def refresh_payment_status(self, save=True):
        if self.direction != self.Direction.IN:
            return
        if self.invoice_amount is None:
            paid = self.amount_paid or Decimal("0.00")
            if paid <= 0:
                return
            self.payment_status = self.PaymentStatus.PARTIAL
            if save:
                self.save(update_fields=["payment_status"])
            return
        paid = self.amount_paid or Decimal("0.00")
        if paid <= 0:
            if self.payment_status == self.PaymentStatus.PAID:
                self.payment_status = self.PaymentStatus.UNPAID
        elif paid >= self.invoice_amount:
            self.payment_status = self.PaymentStatus.PAID
            self.amount_paid = self.invoice_amount
        else:
            self.payment_status = self.PaymentStatus.PARTIAL
        if save:
            self.save(update_fields=["payment_status", "amount_paid"])

    def save(self, *args, **kwargs):
        if not self.reference_code:
            for _ in range(8):
                candidate = generate_stock_movement_reference_code(self.direction)
                if not type(self).objects.filter(reference_code=candidate).exists():
                    self.reference_code = candidate
                    break
            if not self.reference_code:
                self.reference_code = generate_stock_movement_reference_code(
                    self.direction
                )
        super().save(*args, **kwargs)


def get_or_create_store_supplier(*, name: str, phone_number: str, user=None):
    """Find a supplier by phone, or register a new one."""
    phone = normalize_supplier_phone(phone_number)
    cleaned_name = (name or "").strip().upper()
    if not phone:
        raise ValueError("Supplier phone number is required.")
    if not cleaned_name:
        raise ValueError("Supplier name is required.")

    supplier = StoreSupplier.objects.filter(phone_number=phone).first()
    if supplier is None:
        supplier = StoreSupplier.objects.create(
            name=cleaned_name,
            phone_number=phone,
            created_by=user if getattr(user, "is_authenticated", False) else None,
        )
        return supplier, True

    if cleaned_name and supplier.name != cleaned_name:
        supplier.name = cleaned_name
        supplier.save(update_fields=["name", "updated_at"])
    return supplier, False


SUPPLIER_PAYABLE_STATUSES = (
    StoreStockMovement.PaymentStatus.UNPAID,
    StoreStockMovement.PaymentStatus.PENDING,
    StoreStockMovement.PaymentStatus.PARTIAL,
)


def supplier_payable_movements(supplier_id):
    """Open stock-in deliveries the school still owes a supplier for."""
    return (
        StoreStockMovement.objects.filter(
            supplier_id=supplier_id,
            direction=StoreStockMovement.Direction.IN,
            payment_status__in=SUPPLIER_PAYABLE_STATUSES,
            invoice_amount__isnull=False,
        )
        .select_related("item", "item__expense_category")
        .order_by("created_at", "id")
    )


def generate_purchase_invoice_reference_code() -> str:
    import secrets

    stamp = timezone.now().strftime("%Y%m%d")
    return f"PIN-{stamp}-{secrets.token_hex(3).upper()}"


class SupplierPurchaseInvoice(models.Model):
    """Purchase invoice registered against a supplier (accounts payable)."""

    class Status(models.TextChoices):
        UNPAID = "UNPAID", "Unpaid"
        PARTIAL = "PARTIAL", "Partially paid"
        PAID = "PAID", "Paid"

    supplier = models.ForeignKey(
        StoreSupplier,
        on_delete=models.PROTECT,
        related_name="purchase_invoices",
    )
    invoice_number = models.CharField(max_length=120)
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    amount_paid = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    description = models.CharField(max_length=255, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.UNPAID,
    )
    reference_code = models.CharField(max_length=40, unique=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="supplier_purchase_invoices_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "accounts_supplier_purchase_invoice"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.reference_code} · {self.invoice_number} · {self.amount}"

    @property
    def amount_outstanding(self):
        return max(self.amount - (self.amount_paid or Decimal("0.00")), Decimal("0.00"))

    def refresh_status(self, save=True):
        paid = self.amount_paid or Decimal("0.00")
        if paid <= 0:
            self.status = self.Status.UNPAID
        elif paid >= self.amount:
            self.status = self.Status.PAID
            self.amount_paid = self.amount
        else:
            self.status = self.Status.PARTIAL
        if save:
            self.save(update_fields=["status", "amount_paid"])

    def save(self, *args, **kwargs):
        if not self.reference_code:
            for _ in range(8):
                candidate = generate_purchase_invoice_reference_code()
                if not type(self).objects.filter(reference_code=candidate).exists():
                    self.reference_code = candidate
                    break
            if not self.reference_code:
                self.reference_code = generate_purchase_invoice_reference_code()
        super().save(*args, **kwargs)


def supplier_open_purchase_invoices(supplier_id):
    return SupplierPurchaseInvoice.objects.filter(
        supplier_id=supplier_id,
        status__in=[
            SupplierPurchaseInvoice.Status.UNPAID,
            SupplierPurchaseInvoice.Status.PARTIAL,
        ],
    ).order_by("created_at", "id")


def supplier_outstanding_balance(supplier_id) -> Decimal:
    """Total unpaid invoice balance the school owes a supplier."""
    outstanding = Decimal("0.00")
    for movement in supplier_payable_movements(supplier_id):
        outstanding += movement.amount_outstanding or Decimal("0.00")
    for invoice in supplier_open_purchase_invoices(supplier_id):
        outstanding += invoice.amount_outstanding or Decimal("0.00")
    return outstanding


def apply_barter_to_supplier(
    *,
    supplier,
    amount: Decimal,
    account,
    user=None,
    reference: str = "",
    notes: str = "",
) -> Decimal:
    """
    Settle supplier debt by trading against a student fee payment.
    Applies oldest open deliveries first. No cash withdrawal is created.
    """
    from django.db import transaction

    if amount <= 0:
        raise ValueError("Barter amount must be greater than zero.")

    remaining = amount
    settled = Decimal("0.00")
    with transaction.atomic():
        movements = list(
            supplier_payable_movements(supplier.id).select_for_update()
        )
        for movement in movements:
            if remaining <= 0:
                break
            owed = movement.amount_outstanding or Decimal("0.00")
            if owed <= 0:
                continue
            slice_amount = min(remaining, owed)
            movement.amount_paid = (movement.amount_paid or Decimal("0.00")) + slice_amount
            movement.refresh_payment_status(save=False)
            movement.save(update_fields=["amount_paid", "payment_status"])

            payment = StoreSupplierPayment(
                movement=movement,
                account=account,
                amount=slice_amount,
                method=AccountWithdraw.Method.BARTER,
                reference_number=reference or f"BARTER-{supplier.id}",
                created_by=user if getattr(user, "is_authenticated", False) else None,
            )
            payment.save()
            settled += slice_amount
            remaining -= slice_amount

        invoices = list(
            supplier_open_purchase_invoices(supplier.id).select_for_update()
        )
        for invoice in invoices:
            if remaining <= 0:
                break
            owed = invoice.amount_outstanding or Decimal("0.00")
            if owed <= 0:
                continue
            slice_amount = min(remaining, owed)
            invoice.amount_paid = (invoice.amount_paid or Decimal("0.00")) + slice_amount
            invoice.refresh_status(save=False)
            invoice.save(update_fields=["amount_paid", "status"])

            payment = StoreSupplierPayment(
                purchase_invoice=invoice,
                account=account,
                amount=slice_amount,
                method=AccountWithdraw.Method.BARTER,
                reference_number=reference or f"BARTER-{supplier.id}",
                created_by=user if getattr(user, "is_authenticated", False) else None,
            )
            payment.save()
            settled += slice_amount
            remaining -= slice_amount

    if settled <= 0:
        raise ValueError("This supplier has no tradable outstanding balance.")
    if settled < amount:
        raise ValueError(
            f"Only KES {settled:,.2f} is owed to this supplier; reduce the barter amount."
        )
    return settled


def generate_supplier_payment_reference_code() -> str:
    import secrets

    stamp = timezone.now().strftime("%Y%m%d")
    return f"SPY-{stamp}-{secrets.token_hex(3).upper()}"


class StoreSupplierPayment(models.Model):
    """Payment to a supplier for a stock-in delivery or purchase invoice."""

    movement = models.ForeignKey(
        StoreStockMovement,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="supplier_payments",
    )
    purchase_invoice = models.ForeignKey(
        "SupplierPurchaseInvoice",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payments",
    )
    account = models.ForeignKey(
        SchoolAccount,
        on_delete=models.PROTECT,
        related_name="supplier_payments",
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    method = models.CharField(max_length=20, choices=AccountWithdraw.Method.choices)
    reference_number = models.CharField(max_length=120)
    reference_code = models.CharField(max_length=40, unique=True)
    withdrawal = models.OneToOneField(
        AccountWithdraw,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="supplier_payment",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="store_supplier_payments_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "accounts_store_supplier_payment"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.reference_code} · {self.amount}"

    def save(self, *args, **kwargs):
        if bool(self.movement_id) == bool(self.purchase_invoice_id):
            raise ValueError(
                "Supplier payment must link to either a delivery or a purchase invoice."
            )
        if not self.reference_code:
            for _ in range(8):
                candidate = generate_supplier_payment_reference_code()
                if not type(self).objects.filter(reference_code=candidate).exists():
                    self.reference_code = candidate
                    break
            if not self.reference_code:
                self.reference_code = generate_supplier_payment_reference_code()
        if not self.reference_number:
            self.reference_number = self.reference_code
        super().save(*args, **kwargs)


def school_account_available_balance(account) -> Decimal:
    topups = (
        AccountTopUp.objects.filter(
            account=account,
            status=AccountTopUp.Status.APPROVED,
        ).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    withdrawals = (
        AccountWithdraw.objects.filter(
            account=account,
            status=AccountWithdraw.Status.APPROVED,
        ).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    return topups - withdrawals


def store_item_quantity_on_hand(item) -> Decimal:
    incoming = item.movements.filter(
        direction=StoreStockMovement.Direction.IN
    ).aggregate(total=Sum("quantity"))["total"] or Decimal("0.00")
    outgoing = item.movements.filter(
        direction=StoreStockMovement.Direction.OUT
    ).aggregate(total=Sum("quantity"))["total"] or Decimal("0.00")
    return incoming - outgoing


def ensure_store_lookups():
    """Create default expense categories and department stations if missing."""
    if not StoreExpenseCategory.objects.exists():
        StoreExpenseCategory.objects.bulk_create(
            [
                StoreExpenseCategory(name=name, sort_order=index)
                for index, name in enumerate(STORE_EXPENSE_CATEGORY_DEFAULTS, start=1)
            ]
        )
    if not StoreDepartmentStation.objects.exists():
        StoreDepartmentStation.objects.bulk_create(
            [
                StoreDepartmentStation(name=name, sort_order=index)
                for index, name in enumerate(STORE_DEPARTMENT_STATION_DEFAULTS, start=1)
            ]
        )
