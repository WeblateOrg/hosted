#
# Copyright © 2012 - 2021 Michal Čihař <michal@cihar.com>
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

from django.conf import settings
from django.core.management.base import BaseCommand
from django_countries import countries
from fakturace.storage import InvoiceStorage
from weblate.billing.models import Invoice

from wlhosted.integrations.utils import get_origin
from wlhosted.payments.models import Customer, Payment

ALIASES = {
    "The Netherlands": "NL",
    "USA": "US",
    "ČR": "CZ",
}
CUSTOMERS = {
    1: "aptoide",
    2: "web-21",
    3: "braiins",
}


def get_country(text):
    try:
        return ALIASES[text]
    except KeyError:
        for code, name in countries:
            if text == name:
                return code
        raise ValueError(f"Unknown country: {text}")


class Command(BaseCommand):
    help = "migrates payments to include all needed metadata"

    def update_payment(self, invoice):
        payment = Payment.objects.get(pk=invoice.payment["pk"])
        if payment.start:
            return
        self.stdout.write(f"Updating payment info for {payment.pk}")
        payment.start = invoice.start
        payment.end = invoice.end
        payment.save(update_fields=["start", "end"])

    def handle_missing_payment(self, invoice, storage):
        if not invoice.ref:
            self.stderr.write(
                "Missing reference in {} [{}]: {}".format(
                    invoice.pk, invoice.billing.pk, invoice
                )
            )
            if invoice.billing.pk in CUSTOMERS:
                contact = storage.read_contact(CUSTOMERS[invoice.billing.pk])
            else:
                contact = storage.read_contact(f"pp-{invoice.billing.pk}")
        else:
            data = storage.get(invoice.ref)
            contact = data.contact
        if not isinstance(invoice.payment, dict):
            amount = invoice.payment
        else:
            amount = invoice.amount
        if invoice.currency == Invoice.CURRENCY_BTC:
            amount *= 100000
        self.stdout.write(
            "Missing payment for {}, {} {}".format(
                invoice, amount, invoice.get_currency_display()
            )
        )
        # Create fake customer
        customer, created = Customer.objects.get_or_create(
            vat=contact["vat_reg"],
            tax=contact["tax_reg"],
            name=contact["name"],
            address=contact["address"],
            city=contact["city"],
            country=get_country(contact["country"]),
            defaults={
                "user_id": -1,
                "origin": get_origin(),
                "email": contact.get("email", ""),
            },
        )
        if created:
            self.stdout.write(f"Created customer: {customer}")
        # Create payment
        payment = Payment.objects.create(
            amount=amount,
            currency=invoice.currency,
            state=Payment.PROCESSED,
            backend="import",
            customer=customer,
            invoice=invoice.ref,
            start=invoice.start,
            end=invoice.end,
        )
        invoice.payment = {"pk": payment.pk}
        invoice.save(update_fields=["payment"])

    def include_billing_id(self, invoice):
        payment = Payment.objects.get(pk=invoice.payment["pk"])
        if "billing" not in payment.extra:
            self.stdout.write(f"Linking payment: {payment}")
            payment.extra["billing"] = invoice.billing_id
            payment.save(update_fields=["extra"])

    def handle(self, *args, **options):
        storage = InvoiceStorage(settings.PAYMENT_FAKTURACE)
        for invoice in Invoice.objects.all():
            if isinstance(invoice.payment, dict) and "pk" in invoice.payment:
                self.update_payment(invoice)
            else:
                self.handle_missing_payment(invoice, storage)
            self.include_billing_id(invoice)
