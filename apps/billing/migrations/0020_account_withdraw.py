import django.core.validators
import django.db.models.deletion
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0019_schoolaccount_custom_category"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AccountWithdraw",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "amount",
                    models.DecimalField(
                        decimal_places=2,
                        max_digits=12,
                        validators=[django.core.validators.MinValueValidator(Decimal("0.01"))],
                    ),
                ),
                (
                    "method",
                    models.CharField(
                        choices=[
                            ("CASH", "Cash"),
                            ("MPESA", "M-Pesa"),
                            ("BANK", "Bank transfer"),
                            ("CHEQUE", "Cheque"),
                            ("OTHER", "Other"),
                        ],
                        max_length=20,
                    ),
                ),
                (
                    "payee",
                    models.CharField(help_text="Person or party receiving the funds.", max_length=160),
                ),
                ("description", models.CharField(blank=True, max_length=255)),
                ("reference_number", models.CharField(max_length=120)),
                ("reference_code", models.CharField(max_length=40, unique=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("APPROVED", "Approved"),
                            ("PENDING", "Pending"),
                            ("CANCELLED", "Cancelled"),
                        ],
                        default="APPROVED",
                        max_length=20,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="withdrawals",
                        to="billing.schoolaccount",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="account_withdrawals_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "accounts_account_withdraw",
                "ordering": ["-created_at"],
            },
        ),
    ]
