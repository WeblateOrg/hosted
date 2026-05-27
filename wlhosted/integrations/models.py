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

from __future__ import annotations

from typing import TYPE_CHECKING

from appconf import AppConf
from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.db import models, transaction
from django.db.models.aggregates import Max
from django.db.models.signals import post_save, pre_save
from django.dispatch.dispatcher import receiver
from django.utils import timezone
from weblate.auth.models import User
from weblate.billing.models import Billing, BillingEvent, Invoice, Plan
from weblate.utils.decorators import disable_for_loaddata

from wlhosted.payments.models import Payment, get_period_delta

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime


def end_interval(payment: Payment, start: datetime) -> datetime:
    return start + get_period_delta(payment.extra["period"])


def add_billing_owner(billing: Billing, user: User) -> None:
    billing.workspace.add_owner(user)


def get_billing_owners(billing: Billing) -> Iterable[User]:
    return billing.workspace.users_with_permission("workspace.edit_members")


@transaction.atomic
@transaction.atomic(using="payments_db")
def handle_received_payment(payment: Payment) -> Billing | None:  # noqa: PLR0912
    plan: Plan | None = None
    if plan_id := payment.extra.get("plan"):
        # Needed for new payments only
        plan = Plan.objects.get(pk=plan_id)
    if "billing" in payment.extra:
        billing = Billing.objects.select_for_update().get(pk=payment.extra["billing"])
        if billing.removal:
            from wlhosted.integrations.tasks import notify_paid_removal  # noqa: PLC0415

            notify_paid_removal.delay(billing.id)
        billing.removal = None
        billing.state = Billing.STATE_ACTIVE
        if plan is not None:
            billing.plan = plan
        if payment.customer.name and billing.customer_name != payment.customer.name:
            billing.customer_name = payment.customer.name
    elif plan is not None:
        billing = Billing.objects.create(
            state=Billing.STATE_ACTIVE,
            plan=plan,
            customer_name=payment.customer.name,
        )
        add_billing_owner(billing, User.objects.get(pk=payment.customer.user_id))
    else:
        return None

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
    billing.billinglog_set.create(
        event=BillingEvent.PAYMENT, summary=f"Billing paid via {payment.pk}"
    )

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


class UserSyncState(models.Model):
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="hosted_sync_state"
    )
    updated = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        verbose_name = "User sync state"
        verbose_name_plural = "User sync states"

    def __str__(self) -> str:
        return self.user.username


def get_user_sync_profile(user: User) -> dict[str, object]:
    return {
        "username": user.username,
        "last_name": user.last_name,
        "email": user.email,
        "active": user.is_active,
        "is_active": user.is_active,
    }


def normalize_user_sync_changes(changes: dict[str, object]) -> dict[str, object]:
    result = changes.copy()
    if "is_active" in result:
        result["active"] = result["is_active"]
    elif "active" in result:
        result["is_active"] = result["active"]
    return result


def get_user_sync_payload(
    user: User, changes: dict[str, object] | None = None
) -> dict[str, object]:
    profile = get_user_sync_profile(user)
    return {
        "provider": "https://hosted.weblate.org/idp/metadata",
        "external_id": str(user.pk),
        "profile": profile,
        "changes": normalize_user_sync_changes(changes) if changes else profile,
    }


def queue_user_sync(user: User, changes: dict[str, object] | None = None) -> None:
    from wlhosted.integrations.tasks import notify_user_change  # noqa: PLC0415

    if user.is_anonymous or not settings.PAYMENT_SECRET:
        return
    UserSyncState.objects.update_or_create(
        user=user, defaults={"updated": timezone.now()}
    )
    payload = get_user_sync_payload(user, changes)
    transaction.on_commit(lambda: notify_user_change.delay(payload))


@receiver(pre_save, sender=User)
@disable_for_loaddata
def prepare_user_changes(sender, instance, **kwargs) -> None:
    if instance.is_anonymous:
        return
    fields = ("username", "last_name", "email", "is_active")
    changed = {}

    if instance.pk:
        try:
            old = User.objects.get(pk=instance.pk)
        except User.DoesNotExist:
            instance._wlhosted_sync_changes = None
            return
        for field in fields:
            if getattr(old, field) != getattr(instance, field):
                changed[field] = getattr(instance, field)
    instance._wlhosted_sync_changes = changed or None


@receiver(post_save, sender=User)
@disable_for_loaddata
def propagate_user_changes(sender, instance, created=False, **kwargs) -> None:
    if created:
        queue_user_sync(instance)
        return
    changes = getattr(instance, "_wlhosted_sync_changes", None)
    if changes:
        queue_user_sync(instance, changes)
