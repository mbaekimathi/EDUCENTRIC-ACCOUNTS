import django.core.validators
from django.db import migrations, models


def forwards_populate_staff_codes(apps, schema_editor):
    AccountsUser = apps.get_model("staff", "AccountsUser")
    used = set()
    next_code = 100001
    for user in AccountsUser.objects.all().order_by("id"):
        candidate = None
        if user.username and user.username.isdigit() and len(user.username) == 6:
            candidate = user.username
        if not candidate or candidate in used:
            while f"{next_code:06d}" in used:
                next_code += 1
            candidate = f"{next_code:06d}"
            next_code += 1
        used.add(candidate)
        user.staff_code = candidate
        user.save(update_fields=["staff_code"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("staff", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="accountsuser",
            name="staff_code",
            field=models.CharField(
                help_text="Six-digit accounts staff identification code.",
                max_length=6,
                null=True,
                validators=[
                    django.core.validators.RegexValidator(
                        "^\\d{6}$", "Enter exactly six digits."
                    )
                ],
            ),
        ),
        migrations.RunPython(forwards_populate_staff_codes, noop_reverse),
        migrations.AlterField(
            model_name="accountsuser",
            name="staff_code",
            field=models.CharField(
                help_text="Six-digit accounts staff identification code.",
                max_length=6,
                unique=True,
                validators=[
                    django.core.validators.RegexValidator(
                        "^\\d{6}$", "Enter exactly six digits."
                    )
                ],
            ),
        ),
        migrations.RemoveField(
            model_name="accountsuser",
            name="username",
        ),
        migrations.AlterModelOptions(
            name="accountsuser",
            options={
                "ordering": ["staff_code"],
                "verbose_name": "accounts user",
                "verbose_name_plural": "accounts users",
            },
        ),
    ]
