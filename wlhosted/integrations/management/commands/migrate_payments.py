#
# Copyright © 2012 - 2020 Michal Čihař <michal@cihar.com>
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

from django.core.management.base import BaseCommand
from weblate.billing.models import Invoice

from wlhosted.payments.models import Payment


class Command(BaseCommand):
    help = "changes default site name"

    def handle(self, *args, **options):
        for invoice in Invoice.objects.all():
            if isinstance(invoice.payment, dict) and "pk" in invoice.payment:
                payment = Payment.objects.get(pk=invoice.payment["pk"])
                if payment.start:
                    print("Already updated: {}".format(payment.pk))
                    continue
                print("Updating payment info for {}".format(payment.pk))
                payment.start = invoice.start
                payment.end = invoice.end
                payment.save(update_fields=["start", "end"])
            else:
                if not isinstance(invoice.payment, dict):
                    amount = invoice.payment
                else:
                    amount = invoice.amount
                if invoice.currency == Invoice.CURRENCY_BTC:
                    amount *= 10000
                print(
                    "Missing payment for {}, {} {}".format(
                        invoice, amount, invoice.get_currency_display()
                    )
                )
