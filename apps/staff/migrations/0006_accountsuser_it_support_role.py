from django.db import migrations, models


def forwards_rename_support(apps, schema_editor):
    AccountsUser = apps.get_model("staff", "AccountsUser")
    AccountsUser.objects.filter(role="SUPPORT").update(role="IT_SUPPORT")


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("staff", "0005_accountsuser_support_role"),
    ]

    operations = [
        migrations.RunPython(forwards_rename_support, noop_reverse),
        migrations.AlterField(
            model_name="accountsuser",
            name="role",
            field=models.CharField(
                choices=[
                    ("ACCOUNTANT", "Accountant"),
                    ("STORE_MANAGER", "Store Manager"),
                    ("IT_SUPPORT", "IT Support"),
                ],
                default="ACCOUNTANT",
                max_length=20,
            ),
        ),
    ]
