# Generated by Django 4.2.5 on 2023-09-18 11:33

import uuid

import django.core.serializers.json
import django.db.models.deletion
import django_countries.fields
import vies.models
import weblate.utils.validators
from django.db import migrations, models

import wlhosted.payments.validators


class Migration(migrations.Migration):
    replaces = [
        ("payments", "0001_initial"),
        ("payments", "0002_auto_20181024_1151"),
        ("payments", "0003_auto_20181024_1604"),
        ("payments", "0004_auto_20181025_1130"),
        ("payments", "0005_auto_20181025_1135"),
        ("payments", "0006_auto_20181101_0900"),
        ("payments", "0007_customer_tax"),
        ("payments", "0008_auto_20181106_1622"),
        ("payments", "0009_auto_20181113_2339"),
        ("payments", "0010_auto_20181127_1307"),
        ("payments", "0011_payment_amount_fixed"),
        ("payments", "0012_auto_20190102_1658"),
        ("payments", "0013_auto_20190903_1456"),
        ("payments", "0014_auto_20200316_1344"),
        ("payments", "0015_payment_currency"),
        ("payments", "0016_auto_20200318_0955"),
        ("payments", "0017_auto_20200422_2019"),
        ("payments", "0018_auto_20200821_1034"),
        ("payments", "0019_auto_20220915_1301"),
        ("payments", "0020_alter_payment_recurring"),
        ("payments", "0021_payment_details_new_payment_extra_new"),
        ("payments", "0022_jsonfield"),
        ("payments", "0023_remove_payment_details_remove_payment_extra"),
        ("payments", "0024_rename_details_new_payment_details_and_more"),
    ]

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Customer",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "vat",
                    vies.models.VATINField(
                        blank=True,
                        help_text="Please fill in European Union VAT ID, leave blank if not applicable.",
                        max_length=14,
                        null=True,
                        validators=[wlhosted.payments.validators.validate_vatin],
                        verbose_name="European VAT ID",
                    ),
                ),
                (
                    "name",
                    models.CharField(
                        max_length=200,
                        null=True,
                        verbose_name="Company or individual name",
                    ),
                ),
                (
                    "address",
                    models.CharField(max_length=200, null=True, verbose_name="Address"),
                ),
                (
                    "city",
                    models.CharField(
                        max_length=200, null=True, verbose_name="Postcode and city"
                    ),
                ),
                (
                    "country",
                    django_countries.fields.CountryField(
                        max_length=2, null=True, verbose_name="Country"
                    ),
                ),
                (
                    "email",
                    models.EmailField(
                        max_length=190,
                        validators=[weblate.utils.validators.EmailValidator()],
                    ),
                ),
                ("origin", models.URLField(max_length=300)),
                ("user_id", models.IntegerField()),
                (
                    "tax",
                    models.CharField(
                        blank=True,
                        help_text="Please fill in your tax registration if it should appear on the invoice.",
                        max_length=200,
                        verbose_name="Tax registration",
                    ),
                ),
            ],
            options={
                "verbose_name": "Customer",
                "verbose_name_plural": "Customers",
            },
        ),
        migrations.CreateModel(
            name="Payment",
            fields=[
                (
                    "uuid",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("amount", models.IntegerField()),
                ("description", models.TextField()),
                (
                    "recurring",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("y", "Annual"),
                            ("b", "Biannual"),
                            ("q", "Quarterly"),
                            ("m", "Monthly"),
                            ("", "One-time"),
                        ],
                        default="",
                        max_length=10,
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("backend", models.CharField(blank=True, default="", max_length=100)),
                (
                    "repeat",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        to="payments.payment",
                    ),
                ),
                ("invoice", models.CharField(blank=True, default="", max_length=20)),
                (
                    "customer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="payments.customer",
                    ),
                ),
                (
                    "state",
                    models.IntegerField(
                        choices=[
                            (1, "New payment"),
                            (2, "Awaiting payment"),
                            (3, "Payment rejected"),
                            (4, "Payment accepted"),
                            (5, "Payment processed"),
                        ],
                        db_index=True,
                        default=1,
                    ),
                ),
                ("amount_fixed", models.BooleanField(blank=True, default=False)),
                ("end", models.DateField(blank=True, null=True)),
                ("start", models.DateField(blank=True, null=True)),
                (
                    "currency",
                    models.IntegerField(
                        choices=[(0, "EUR"), (1, "BTC"), (2, "USD"), (3, "CZK")],
                        default=0,
                    ),
                ),
                (
                    "details",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        encoder=django.core.serializers.json.DjangoJSONEncoder,
                    ),
                ),
                (
                    "extra",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        encoder=django.core.serializers.json.DjangoJSONEncoder,
                    ),
                ),
            ],
            options={
                "ordering": ["-created"],
                "verbose_name": "Payment",
                "verbose_name_plural": "Payments",
            },
        ),
    ]
