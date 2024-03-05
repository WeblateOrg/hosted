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

from datetime import timedelta

import requests
from celery.schedules import crontab
from django.conf import settings
from django.core.signing import dumps
from django.db import transaction
from django.utils import timezone
from weblate.accounts.notifications import send_notification_email
from weblate.billing.models import Billing
from weblate.utils.celery import app

from wlhosted.integrations.models import handle_received_payment
from wlhosted.integrations.utils import get_origin
from wlhosted.payments.models import Payment


@app.task
def pending_payments():
    with transaction.atomic(using="payments_db"):
        payments = Payment.objects.filter(
            customer__origin=get_origin(), state=Payment.ACCEPTED
        ).select_for_update()
        for payment in payments:
            handle_received_payment(payment)


@app.task
def notify_paid_removal(billing_id):
    billing = Billing.objects.get(pk=billing_id)
    for user in billing.get_notify_users():
        send_notification_email(
            user.profile.language,
            [user.email],
            "billing_paid",
            context={"billing": billing},
            info=billing,
        )


@app.task
def recurring_payments():
    cutoff = timezone.now().date() + timedelta(days=1)
    for billing in Billing.objects.filter(state=Billing.STATE_ACTIVE).select_related(
        "plan"
    ):
        if "recurring" not in billing.payment:
            continue
        last_invoice = billing.invoice_set.order_by("-start")[0]
        if last_invoice.end > cutoff:
            continue

        original = Payment.objects.get(pk=billing.payment["recurring"])

        repeated = original.repeat_payment(
            amount=billing.plan.price
            if original.extra["period"] == "m"
            else billing.plan.yearly_price,
            billing=billing.pk,
        )
        if not repeated:
            # Remove recurring flag
            del billing.payment["recurring"]
            billing.save()
        else:
            repeated.trigger_remotely()

    # We have created bunch of pending payments, process them now
    pending_payments()


@app.task
def notify_user_change(username, changes, create):
    if not settings.PAYMENT_SECRET:
        return
    response = requests.post(
        "https://weblate.org/api/user/",
        data={
            "payload": dumps(
                {"username": username, "create": create, "changes": changes},
                key=settings.PAYMENT_SECRET,
                salt="weblate.user",
            )
        },
    )
    response.raise_for_status()


@app.on_after_finalize.connect
def setup_periodic_tasks(sender, **kwargs):
    sender.add_periodic_task(300, pending_payments.s(), name="pending-payments")
    sender.add_periodic_task(
        crontab(hour=8, minute=0), recurring_payments.s(), name="recurring-payments"
    )
