# Generated by Django 5.0.4 on 2024-12-18 15:10

import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0027_customer_address_2_customer_postcode_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="customer",
            name="created",
            field=models.DateTimeField(
                auto_now_add=True, default=django.utils.timezone.now
            ),
            preserve_default=False,
        ),
    ]