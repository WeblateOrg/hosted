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

from appconf import AppConf
from dateutil.relativedelta import relativedelta
from django.db import transaction
from django.db.models.aggregates import Max
from django.db.models.signals import pre_save
from django.dispatch.dispatcher import receiver
from django.utils import timezone
from weblate.auth.models import User
from weblate.billing.models import Billing, Invoice, Plan
from weblate.utils.decorators import disable_for_loaddata

from wlhosted.payments.models import Payment, get_period_delta


def end_interval(payment, start):
    return start + get_period_delta(payment.extra["period"])


@transaction.atomic(using="payments_db")
def handle_received_payment(payment):
    params = {
        "plan": Plan.objects.get(pk=payment.extra["plan"]),
        "state": Billing.STATE_ACTIVE,
        "removal": None,
    }
    if "billing" in payment.extra:
        billing = Billing.objects.get(pk=payment.extra["billing"])
        if billing.removal:
            from wlhosted.integrations.tasks import notify_paid_removal

            notify_paid_removal(billing.id)
        for key, value in params.items():
            setattr(billing, key, value)
    else:
        billing = Billing.objects.create(**params)
        billing.owners.add(User.objects.get(pk=payment.customer.user_id))

    # Update recurrence information
    if payment.recurring:
        billing.payment["recurring"] = payment.pk
    elif payment.repeat:
        billing.payment["recurring"] = payment.repeat.pk
    elif "recurring" in billing.payment:
        del billing.payment["recurring"]
    # Store all payment links
    if "all" not in billing.payment:
        billing.payment["all"] = []
    billing.payment["all"].append(payment.pk)

    billing.save()

    start = billing.invoice_set.aggregate(Max("end"))["end__max"]
    if start is not None:
        start += relativedelta(days=1)
    else:
        start = timezone.now()

    Invoice.objects.create(
        billing=billing,
        start=start,
        end=end_interval(payment, start),
        amount=payment.vat_amount,
        currency=Invoice.CURRENCY_EUR,
        ref=payment.invoice,
        payment={"pk": str(payment.pk)},
    )

    payment.state = Payment.PROCESSED
    payment.save()

    return billing


class HostedConf(AppConf):
    REDIRECT_URL = "https://weblate.org/{language}/payment/{uuid}/"
    ENABLED = True

    class Meta:
        prefix = "PAYMENT"


@receiver(pre_save, sender=User)
@disable_for_loaddata
def propagate_user_changes(sender, instance, **kwargs):
    from wlhosted.integrations.tasks import notify_user_change

    if instance.is_anonymous:
        return
    fields = ("username", "last_name", "email")
    create = {}
    changed = {}
    username = instance.username
    for field in fields:
        create[field] = getattr(instance, field)

    if instance.pk:
        old = User.objects.get(pk=instance.pk)
        username = old.username
        for field in ("username", "last_name", "email"):
            if getattr(old, field) != getattr(instance, field):
                changed[field] = getattr(instance, field)

    notify_user_change.delay(username, changed, create)
