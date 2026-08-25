from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0017_stk_push_request"),
    ]

    operations = [
        migrations.AlterField(
            model_name="schoolaccount",
            name="category",
            field=models.CharField(
                choices=[
                    ("STUDENT_FEES", "Student fees"),
                    ("POCKET_MONEY", "Pocket money"),
                    ("PETTY_CASHBOOK", "Petty cashbook"),
                    ("OPERATIONS", "Operations"),
                    ("CAPITAL", "Capital"),
                    ("OTHER", "Other"),
                ],
                max_length=32,
            ),
        ),
    ]
