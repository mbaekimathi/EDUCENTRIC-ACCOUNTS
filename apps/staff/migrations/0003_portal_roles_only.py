from django.db import migrations, models


LEGACY_TO_ACCOUNTS = ("BURSAR", "CASHIER", "AUDITOR", "ADMIN")


def forwards_normalize_roles(apps, schema_editor):
    AccountsUser = apps.get_model("staff", "AccountsUser")
    AccountsUser.objects.filter(role__in=LEGACY_TO_ACCOUNTS).update(role="ACCOUNTS")
    AccountsUser.objects.exclude(role__in=("ACCOUNTS", "STORE_MANAGER")).update(
        role="ACCOUNTS"
    )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("staff", "0002_staff_code_login"),
    ]

    operations = [
        migrations.RunPython(forwards_normalize_roles, noop_reverse),
        migrations.AlterField(
            model_name="accountsuser",
            name="role",
            field=models.CharField(
                choices=[
                    ("ACCOUNTS", "Accounts"),
                    ("STORE_MANAGER", "Store Manager"),
                ],
                default="ACCOUNTS",
                max_length=20,
            ),
        ),
    ]
