from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("directory", "0002_employee_mirror"),
    ]

    operations = [
        migrations.CreateModel(
            name="AcademicLevel",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120)),
                ("code", models.CharField(max_length=40)),
                ("description", models.TextField(blank=True)),
                ("order", models.PositiveIntegerField(default=0)),
                ("status", models.CharField(blank=True, max_length=10)),
                ("category", models.CharField(blank=True, max_length=120)),
            ],
            options={
                "db_table": "curriculum_academiclevel",
                "ordering": ["order", "name"],
                "managed": False,
            },
        ),
    ]
