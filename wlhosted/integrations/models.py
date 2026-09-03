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

import uuid
from itertools import batched
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
from weblate.billing.models import (
    Billing,
    BillingEvent,
    Invoice,
    Plan,
    get_payment_log_details,
    get_plan_change_log_details,
)
from weblate.utils.decorators import disable_for_loaddata

from wlhosted.payments.models import Customer, Payment, get_period_delta

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime


SYNC_QUERY_BATCH_SIZE = 1000
PAYMENT_QUERY_BATCH_SIZE = 1000


def end_interval(payment: Payment, start: datetime) -> datetime:
    return start + get_period_delta(payment.extra["period"])


def add_billing_owner(billing: Billing, user: User) -> None:
    billing.workspace.add_owner(user)


def get_billing_owners(billing: Billing) -> Iterable[User]:
    return billing.workspace.users_with_permission("workspace.edit_members")


def normalize_payment_pk(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        return None


def get_billing_payment_pks(
    billing: Billing, invoice_payments: Iterable[object]
) -> Iterable[uuid.UUID]:
    for invoice_payment in invoice_payments:
        if isinstance(invoice_payment, dict) and (
            payment_pk := normalize_payment_pk(invoice_payment.get("pk"))
        ):
            yield payment_pk

    if not isinstance(billing.payment, dict):
        return

    payment_pks = billing.payment.get("all", [])
    if isinstance(payment_pks, list):
        for value in reversed(payment_pks):
            if payment_pk := normalize_payment_pk(value):
                yield payment_pk

    if payment_pk := normalize_payment_pk(billing.payment.get("recurring")):
        yield payment_pk


def get_billing_payment(
    payment_pks: Iterable[uuid.UUID], customer: Customer | None
) -> Payment | None:
    for payment_pk_batch in batched(payment_pks, PAYMENT_QUERY_BATCH_SIZE):
        payments = {
            payment.pk: payment
            for payment in Payment.objects.filter(
                pk__in=payment_pk_batch
            ).select_related("customer")
        }
        for payment_pk in payment_pk_batch:
            payment = payments.get(payment_pk)
            if payment is None:
                continue
            if customer is not None and payment.customer_id != customer.pk:
                return None
            if not payment.customer.name:
                return None
            return payment
    return None


def sync_billing_customer_name(
    billing_id: int, customer: Customer | None = None
) -> None:
    with transaction.atomic():
        try:
            billing = Billing.objects.select_for_update().get(pk=billing_id)
        except Billing.DoesNotExist:
            return
        invoice_payments = (
            Invoice.objects.filter(billing_id=billing.pk)
            .order_by("-start", "-pk")
            .values_list("payment", flat=True)
            .iterator(chunk_size=SYNC_QUERY_BATCH_SIZE)
        )
        payment = get_billing_payment(
            get_billing_payment_pks(billing, invoice_payments), customer
        )
        if payment is not None and billing.customer_name != payment.customer.name:
            billing.customer_name = payment.customer.name
            billing.save(update_fields=["customer_name"])


def sync_billing_customer_names(customer: Customer | None = None) -> None:
    if customer is not None and not customer.name:
        return

    last_billing_id = 0
    while billings := list(
        Billing.objects.filter(pk__gt=last_billing_id).order_by("pk")[
            :SYNC_QUERY_BATCH_SIZE
        ]
    ):
        last_billing_id = billings[-1].pk
        for billing in billings:
            invoice_payments = (
                Invoice.objects.filter(billing_id=billing.pk)
                .order_by("-start", "-pk")
                .values_list("payment", flat=True)
                .iterator(chunk_size=SYNC_QUERY_BATCH_SIZE)
            )
            payment = get_billing_payment(
                get_billing_payment_pks(billing, invoice_payments), customer
            )
            if payment is not None and billing.customer_name != payment.customer.name:
                sync_billing_customer_name(billing.pk, customer)


def get_payment_user(payment: Payment) -> User | None:
    if payment.repeat_id:
        return None
    return User.objects.filter(pk=payment.customer.user_id).first()


def get_payment_plan(payment: Payment, billing: Billing) -> Plan:
    plan_id = payment.extra.get("plan")
    if plan_id:
        return Plan.objects.get(pk=plan_id)
    return billing.plan


def get_hosted_payment_log_details(
    payment: Payment,
    billing: Billing,
    outcome: str,
    *,
    reason: str = "",
) -> dict[str, object]:
    return get_payment_log_details(
        payment.pk,
        get_payment_plan(payment, billing),
        payment.extra.get("period", ""),
        automatic=bool(payment.repeat_id),
        outcome=outcome,
        reason=reason,
    )


def log_rejected_payment(payment: Payment) -> None:
    if payment.extra.get("billing_rejection_logged"):
        return
    billing_id = payment.extra.get("billing")
    if not billing_id:
        payment.extra = {**payment.extra, "billing_rejection_logged": True}
        payment.save(update_fields=["extra"])
        return
    try:
        billing = Billing.objects.select_related("plan").get(pk=billing_id)
    except Billing.DoesNotExist:
        payment.extra = {**payment.extra, "billing_rejection_logged": True}
        payment.save(update_fields=["extra"])
        return
    payment_id = str(payment.pk)
    if not billing.billinglog_set.filter(
        event=BillingEvent.PAYMENT_REJECTED,
        details__payment_id=payment_id,
    ).exists():
        billing.billinglog_set.create(
            event=BillingEvent.PAYMENT_REJECTED,
            summary=f"Payment rejected via {payment.pk}",
            details=get_hosted_payment_log_details(
                payment,
                billing,
                "rejected",
                reason=payment.details.get("reject_reason", ""),
            ),
            user=get_payment_user(payment),
        )
    payment.extra = {**payment.extra, "billing_rejection_logged": True}
    payment.save(update_fields=["extra"])


@transaction.atomic
@transaction.atomic(using="payments_db")
def handle_received_payment(payment: Payment) -> Billing | None:  # noqa: PLR0912
    plan: Plan | None = None
    if plan_id := payment.extra.get("plan"):
        # Needed for new payments only
        plan = Plan.objects.get(pk=plan_id)
    plan_change_details = {}
    if "billing" in payment.extra:
        billing = Billing.objects.select_for_update().get(pk=payment.extra["billing"])
        if billing.removal:
            from wlhosted.integrations.tasks import notify_paid_removal  # noqa: PLC0415

            notify_paid_removal.delay(billing.id)
        billing.removal = None
        billing.state = Billing.STATE_ACTIVE
        if plan is not None:
            old_plan = billing.plan
            if (old_plan_id := billing.plan_id) and old_plan_id != plan.pk:
                plan_change_details = get_plan_change_log_details(old_plan, plan)
            billing.plan = plan
        if payment.customer.name and billing.customer_name != payment.customer.name:
            billing.customer_name = payment.customer.name
    elif plan is not None:
        billing = Billing.objects.create(
            state=Billing.STATE_ACTIVE,
            plan=plan,
            customer_name=payment.customer.name,
        )
        user = User.objects.get(pk=payment.customer.user_id)
        add_billing_owner(billing, user)
        billing.billinglog_set.create(event=BillingEvent.CREATED, user=user)
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
    payment_details = get_hosted_payment_log_details(payment, billing, "received")
    payment_details.update(plan_change_details)
    billing.billinglog_set.create(
        event=BillingEvent.PAYMENT,
        summary=f"Billing paid via {payment.pk}",
        details=payment_details,
        user=get_payment_user(payment),
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
        amount=float(payment.vat_amount),
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


@receiver(pre_save, sender=Customer)
@disable_for_loaddata
def prepare_customer_name_change(
    sender, instance, update_fields=None, using=None, **kwargs
):
    instance._wlhosted_customer_name_changed = False
    if not instance.pk or (update_fields is not None and "name" not in update_fields):
        return
    try:
        old_name = (
            Customer.objects.using(using or "payments_db")
            .values_list("name", flat=True)
            .get(pk=instance.pk)
        )
    except Customer.DoesNotExist:
        return
    instance._wlhosted_customer_name_changed = old_name != instance.name


@receiver(post_save, sender=Customer)
@disable_for_loaddata
def propagate_customer_name(sender, instance, created=False, using=None, **kwargs):
    if created or not getattr(instance, "_wlhosted_customer_name_changed", False):
        return
    transaction.on_commit(
        lambda: sync_billing_customer_names(instance), using=using or "payments_db"
    )
