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

from django.core.management.base import BaseCommand
from weblate.billing.models import Billing
from weblate.utils.site import get_site_url

from wlhosted.payments.models import Payment


class Command(BaseCommand):
    help = "lists payments using obsolete payment method"

    def handle(self, *args, **options):
        for billing in Billing.objects.filter(state=Billing.STATE_ACTIVE):
            if "recurring" not in billing.payment:
                continue
            payment = Payment.objects.get(pk=billing.payment["recurring"])
            if payment.details["methodId"] != "21":
                continue
            self.stdout.write(
                "{} {}, expires {} [{}]: {}".format(
                    get_site_url(billing.get_absolute_url()),
                    billing,
                    billing.invoice_set.all().order_by("-end")[0].end,
                    payment.extra["period"],
                    ", ".join(
                        billing.get_notify_users().values_list("email", flat=True)
                    ),
                )
            )
