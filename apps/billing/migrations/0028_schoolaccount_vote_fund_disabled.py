from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0027_payment_barter_trade"),
    ]

    operations = [
        migrations.AlterField(
            model_name="schoolaccount",
            name="vote_fund_allocation",
            field=models.CharField(
                choices=[
                    ("EQUAL", "Equally among votes"),
                    ("PRIORITY_ORDER", "By allocation order (priority)"),
                    ("DISABLED", "Disable votes"),
                ],
                default="PRIORITY_ORDER",
                help_text="How incoming funds are allocated across votes on this account.",
                max_length=32,
            ),
        ),
    ]
