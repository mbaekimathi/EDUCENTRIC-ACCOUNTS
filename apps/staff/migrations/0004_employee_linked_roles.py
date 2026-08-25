import django.core.validators
from django.db import migrations, models


def forwards_roles_and_employee_link(apps, schema_editor):
    AccountsUser = apps.get_model("staff", "AccountsUser")
    AccountsUser.objects.filter(role="ACCOUNTS").update(role="ACCOUNTANT")
    AccountsUser.objects.filter(role__in=("BURSAR", "CASHIER", "AUDITOR", "ADMIN")).update(
        role="ACCOUNTANT"
    )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("staff", "0003_portal_roles_only"),
    ]

    operations = [
        migrations.RunPython(forwards_roles_and_employee_link, noop_reverse),
        migrations.AlterField(
            model_name="accountsuser",
            name="role",
            field=models.CharField(
                choices=[
                    ("ACCOUNTANT", "Accountant"),
                    ("STORE_MANAGER", "Store Manager"),
                ],
                default="ACCOUNTANT",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="accountsuser",
            name="employee_id",
            field=models.PositiveBigIntegerField(
                blank=True,
                help_text="employees_employee.id from ADMINISTRATION",
                null=True,
                unique=True,
            ),
        ),
        migrations.AlterField(
            model_name="accountsuser",
            name="staff_code",
            field=models.CharField(
                help_text="Matches employees_employee.employee_code.",
                max_length=6,
                unique=True,
                validators=[
                    django.core.validators.RegexValidator(
                        r"^\d{6}$", "Enter exactly six digits."
                    )
                ],
            ),
        ),
    ]
