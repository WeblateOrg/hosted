# Generated by Django 3.0.4 on 2020-03-16 12:49

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0013_auto_20190903_1456"),
    ]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="end",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="payment",
            name="start",
            field=models.DateField(blank=True, null=True),
        ),
    ]