#
# Copyright © Michal Čihař <michal@weblate.org>
#
# This file is part of Weblate <https://weblate.org/>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

import os.path
import uuid

import requests
from appconf import AppConf
from dateutil.relativedelta import relativedelta
from datetime import datetime
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models, transaction
from django.utils.functional import cached_property
from django.utils.translation import get_language, gettext_lazy, pgettext_lazy
from django_countries.fields import CountryField
from vies.models import VATINField
from weblate.utils.validators import validate_email

from wlhosted.data import SUPPORTED_LANGUAGES
from wlhosted.payments.validators import validate_vatin

EU_VAT_RATES = {
    "BE": 21,
    "BG": 20,
    "CZ": 21,
    "DK": 25,
    "DE": 19,
    "EE": 20,
    "IE": 23,
    "GR": 24,
    "ES": 21,
    "FR": 20,
    "HR": 25,
    "IT": 22,
    "CY": 19,
    "LV": 21,
    "LT": 21,
    "LU": 17,
    "HU": 27,
    "MT": 18,
    "NL": 21,
    "AT": 20,
    "PL": 23,
    "PT": 23,
    "RO": 19,
    "SI": 22,
    "SK": 20,
    "FI": 24,
    "SE": 25,
}

VAT_RATE = 21


class Char32UUIDField(models.UUIDField):
    def db_type(self, connection):
        return "char(32)"

    def get_db_prep_value(self, value, connection, prepared=False):
        value = super().get_db_prep_value(value, connection, prepared)
        if value is not None:
            value = value.replace("-", "") if isinstance(value, str) else value.hex
        return value


class Customer(models.Model):
    vat = VATINField(
        validators=[validate_vatin],
        blank=True,
        default="",
        verbose_name=gettext_lazy("European VAT ID"),
        help_text=gettext_lazy(
            "Please fill in European Union VAT ID, leave blank if not applicable."
        ),
    )
    tax = models.CharField(
        max_length=200,
        blank=True,
        verbose_name=gettext_lazy("Tax registration"),
        help_text=gettext_lazy(
            "Please fill in your tax registration if it should "
            "appear on the invoice."
        ),
    )
    name = models.CharField(
        max_length=200,
        default="",
        verbose_name=gettext_lazy("Company or individual name"),
    )
    address = models.CharField(
        max_length=200,
        default="",
        verbose_name=gettext_lazy("Address"),
    )
    address_2 = models.CharField(
        max_length=200,
        default="",
        verbose_name=gettext_lazy("Additional address information"),
        blank=True,
    )
    city = models.CharField(
        max_length=200,
        default="",
        verbose_name=gettext_lazy("Postcode and city"),
    )
    postcode = models.CharField(
        max_length=20,
        default="",
        verbose_name=gettext_lazy("Postcode"),
    )
    country = CountryField(
        default="",
        verbose_name=gettext_lazy("Country"),
    )
    email = models.EmailField(
        blank=True,
        max_length=190,
        validators=[validate_email],
    )
    origin = models.URLField(max_length=300)
    user_id = models.IntegerField()

    class Meta:
        verbose_name = "Customer"
        verbose_name_plural = "Customers"

    def __str__(self):
        if self.name:
            return f"{self.name} ({self.email})"
        return self.email

    @property
    def country_code(self):
        if self.country:
            return self.country.code.upper()
        return None

    @property
    def vat_country_code(self):
        if self.vat:
            if hasattr(self.vat, "country_code"):
                return self.vat.country_code.upper()
            return self.vat[:2].upper()
        return None

    def clean(self):
        if self.vat and self.vat_country_code != self.country_code:
            raise ValidationError(
                {"country": gettext_lazy("The country has to match your VAT code")}
            )

    @property
    def is_empty(self):
        return not (self.name and self.address and self.city and self.country)

    @property
    def is_eu_enduser(self):
        return self.country_code in EU_VAT_RATES and not self.vat

    @property
    def needs_vat(self):
        return self.vat_country_code == "CZ" or self.is_eu_enduser

    @property
    def vat_rate(self):
        if self.needs_vat:
            return VAT_RATE
        return 0


RECURRENCE_CHOICES = [
    ("y", gettext_lazy("Annual")),
    ("b", gettext_lazy("Biannual")),
    ("q", gettext_lazy("Quarterly")),
    ("m", gettext_lazy("Monthly")),
    ("", gettext_lazy("One-time")),
]


class Payment(models.Model):
    NEW = 1
    PENDING = 2
    REJECTED = 3
    ACCEPTED = 4
    PROCESSED = 5

    CURRENCY_EUR = 0
    CURRENCY_BTC = 1
    CURRENCY_USD = 2
    CURRENCY_CZK = 3

    uuid = Char32UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    amount = models.IntegerField()
    currency = models.IntegerField(
        choices=(
            (CURRENCY_EUR, "EUR"),
            (CURRENCY_BTC, "BTC"),
            (CURRENCY_USD, "USD"),
            (CURRENCY_CZK, "CZK"),
        ),
        default=CURRENCY_EUR,
    )
    description = models.TextField()
    recurring = models.CharField(
        choices=RECURRENCE_CHOICES, default="", blank=True, max_length=10
    )
    created = models.DateTimeField(auto_now_add=True)
    state = models.IntegerField(
        choices=[
            (NEW, pgettext_lazy("Payment state", "New payment")),
            (PENDING, pgettext_lazy("Payment state", "Awaiting payment")),
            (REJECTED, pgettext_lazy("Payment state", "Payment rejected")),
            (ACCEPTED, pgettext_lazy("Payment state", "Payment accepted")),
            (PROCESSED, pgettext_lazy("Payment state", "Payment processed")),
        ],
        db_index=True,
        default=NEW,
    )
    backend = models.CharField(max_length=100, default="", blank=True)
    # Payment details from the gateway
    details = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    # Payment extra information from the origin
    extra = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    customer = models.ForeignKey(Customer, on_delete=models.deletion.CASCADE)
    repeat = models.ForeignKey(
        "Payment", on_delete=models.deletion.CASCADE, null=True, blank=True
    )
    invoice = models.CharField(max_length=20, blank=True, default="")
    amount_fixed = models.BooleanField(blank=True, default=False)
    start = models.DateField(blank=True, null=True)
    end = models.DateField(blank=True, null=True)

    class Meta:
        ordering = ["-created"]
        verbose_name = "Payment"
        verbose_name_plural = "Payments"

    def __str__(self):
        return f"payment:{self.pk}"

    @cached_property
    def invoice_filename(self):
        return f"{self.invoice}.pdf"

    @cached_property
    def invoice_full_filename(self):
        return os.path.join(
            settings.PAYMENT_FAKTURACE,
            "proforma" if self.state == self.PENDING else "pdf",
            self.invoice_filename,
        )

    @cached_property
    def invoice_filename_valid(self):
        return os.path.exists(self.invoice_full_filename)

    @property
    def vat_amount(self):
        if self.customer.needs_vat and not self.amount_fixed:
            rate = 100 + self.customer.vat_rate
            return round(1.0 * rate * self.amount / 100, 2)
        return self.amount

    @property
    def amount_without_vat(self):
        if self.customer.needs_vat and self.amount_fixed:
            return 100.0 * self.amount / (100 + self.customer.vat_rate)
        return self.amount

    def get_payment_url(self):
        language = get_language()
        if language not in SUPPORTED_LANGUAGES:
            language = "en"
        return settings.PAYMENT_REDIRECT_URL.format(language=language, uuid=self.uuid)

    def repeat_payment(
        self,
        skip_previous: bool = False,
        amount: int | None = None,
        description: str | None = None,
        **kwargs,
    ):
        # Check if backend is still valid
        from wlhosted.payments.backends import get_backend

        try:
            get_backend(self.backend)
        except KeyError:
            return False

        if description is None:
            description = self.description

        with transaction.atomic(using="payments_db"):
            # Check for failed payments
            previous = Payment.objects.filter(repeat=self)
            if not skip_previous and previous.exists():
                failures = previous.filter(state=Payment.REJECTED)
                try:
                    last_good = previous.filter(state=Payment.PROCESSED).order_by(
                        "-created"
                    )[0]
                    failures = failures.filter(created__gt=last_good.created)
                except IndexError:
                    pass
                if failures.count() >= 3:
                    return False

            # Create new payment object
            extra = {}
            extra.update(self.extra)
            extra.update(kwargs)
            return Payment.objects.create(
                amount=self.amount if amount is None else amount,
                backend=self.backend,
                description=description,
                recurring="",
                customer=self.customer,
                amount_fixed=self.amount_fixed,
                repeat=self,
                extra=extra,
            )

    def trigger_remotely(self):
        # Trigger payment processing remotely
        requests.post(
            self.get_payment_url(),
            allow_redirects=False,
            data={"method": self.backend, "secret": settings.PAYMENT_SECRET},
        )


class PaymentConf(AppConf):
    DEBUG = False
    SECRET = ""
    FAKTURACE = None
    THEPAY_MERCHANTID = None
    THEPAY_ACCOUNTID = None
    THEPAY_PASSWORD = None
    THEPAY_DATAAPI = None
    FIO_TOKEN = None

    class Meta:
        prefix = "PAYMENT"


def get_period_delta(period):
    if period == "y":
        return relativedelta(years=1) - relativedelta(days=1)
    if period == "b":
        return relativedelta(months=6) - relativedelta(days=1)
    if period == "q":
        return relativedelta(months=3) - relativedelta(days=1)
    if period == "m":
        return relativedelta(months=1) - relativedelta(days=1)
    raise ValueError(f"Invalid payment period {period!r}!")


def date_format(value: datetime) -> str:
    return value.strftime("%-d %b %Y")
