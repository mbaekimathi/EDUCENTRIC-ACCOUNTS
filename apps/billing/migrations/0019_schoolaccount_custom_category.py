from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0018_school_account_petty_cashbook_category"),
    ]

    operations = [
        migrations.AddField(
            model_name="schoolaccount",
            name="custom_category",
            field=models.CharField(blank=True, max_length=120),
        ),
    ]
