from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("staff", "0004_employee_linked_roles"),
    ]

    operations = [
        migrations.AlterField(
            model_name="accountsuser",
            name="role",
            field=models.CharField(
                choices=[
                    ("ACCOUNTANT", "Accountant"),
                    ("STORE_MANAGER", "Store Manager"),
                    ("SUPPORT", "Support"),
                ],
                default="ACCOUNTANT",
                max_length=20,
            ),
        ),
    ]
