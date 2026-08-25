from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("directory", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="Employee",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("password", models.CharField(max_length=128)),
                ("last_login", models.DateTimeField(blank=True, null=True)),
                ("is_superuser", models.BooleanField(default=False)),
                ("is_staff", models.BooleanField(default=False)),
                ("is_active", models.BooleanField(default=False)),
                ("date_joined", models.DateTimeField()),
                ("employee_code", models.CharField(max_length=6, unique=True)),
                ("title", models.CharField(blank=True, max_length=4)),
                ("first_name", models.CharField(max_length=150)),
                ("last_name", models.CharField(max_length=150)),
                ("email", models.EmailField(max_length=254)),
                ("phone_number", models.CharField(blank=True, max_length=24)),
                ("profile_image", models.CharField(blank=True, max_length=100)),
                ("approval_status", models.CharField(max_length=20)),
                ("role", models.CharField(max_length=32)),
                ("is_suspended", models.BooleanField(default=False)),
                ("employment_number", models.PositiveIntegerField(blank=True, null=True)),
            ],
            options={
                "db_table": "employees_employee",
                "ordering": ["last_name", "first_name"],
                "managed": False,
            },
        ),
    ]
