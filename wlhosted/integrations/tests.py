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

from time import sleep
from unittest.mock import patch
from urllib.parse import parse_qs

import responses
from dateutil.relativedelta import relativedelta
from django.contrib.auth.hashers import make_password
from django.core import mail
from django.core.signing import dumps, loads
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse
from django.utils import timezone
from weblate.auth.models import User
from weblate.billing.models import Billing, Invoice, Plan
from weblate.trans.models import Project

from wlhosted.integrations.models import (
    UserSyncState,
    get_user_sync_payload,
    queue_user_sync,
)
from wlhosted.integrations.tasks import (
    notify_user_change,
    pending_payments,
    recurring_payments,
)
from wlhosted.payments.backends import get_backend
from wlhosted.payments.models import Customer, Payment

TESTPASSWORD = make_password("testpassword")
TEST_PAYMENT_SECRET = "secret"  # noqa: S105


def create_test_user() -> User:
    return User.objects.create(
        username="testuser",
        email="weblate@example.org",
        password=TESTPASSWORD,
        full_name="Weblate Test",
    )


class PaymentTest(TestCase):
    databases = "__all__"

    def setUp(self) -> None:
        Payment.objects.all().delete()
        Customer.objects.all().delete()
        self.user = create_test_user()
        self.client.login(
            username="testuser",
            password="testpassword",  # noqa: S106
        )
        self.plan_a = Plan.objects.create(
            name="Plan A", slug="plan-a", price=19, yearly_price=199, public=True
        )
        self.plan_b = Plan.objects.create(
            name="Plan B", slug="plan-b", price=49, yearly_price=499, public=True
        )
        self.plan_c = Plan.objects.create(
            name="Plan C", slug="plan-c", price=9, yearly_price=99, public=False
        )
        self.plan_d = Plan.objects.create(
            name="Plan D", slug="plan-d", price=0, yearly_price=0, public=True
        )

    @override_settings(PAYMENT_REDIRECT_URL="http://example.com/payment")
    def create_payment(self, **kwargs) -> None:
        params = {"plan": self.plan_a.id, "period": "y"}
        params.update(kwargs)
        response = self.client.post(reverse("create-billing"), params)
        self.assertRedirects(
            response, "http://example.com/payment", fetch_redirect_response=False
        )

    def create_trial(self):
        bill = Billing.objects.create(state=Billing.STATE_TRIAL, plan=self.plan_b)
        bill.owners.add(self.user)
        project = Project.objects.create(name="Project", slug="project")
        bill.projects.add(project)
        project.add_user(self.user)
        return bill

    def test_create(self) -> None:
        response = self.client.get(reverse("create-billing"))
        self.assertContains(response, "Plan A")
        self.assertContains(response, "Plan B")
        self.assertNotContains(response, "Plan C")
        self.assertNotContains(response, "Plan D")
        self.create_payment(period="y")
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(Customer.objects.count(), 1)
        payment = Payment.objects.all()[0]
        self.assertEqual(payment.amount, self.plan_a.yearly_price)
        self.assertEqual(payment.extra, {"plan": self.plan_a.pk, "period": "y"})
        self.create_payment(period="m")
        self.assertEqual(Payment.objects.count(), 2)
        self.assertEqual(Customer.objects.count(), 1)
        payment = Payment.objects.exclude(uuid=payment.uuid)[0]
        self.assertEqual(payment.amount, self.plan_a.price)

    def test_user_sync_payload(self) -> None:
        self.assertEqual(
            get_user_sync_payload(self.user),
            {
                "provider": "https://hosted.weblate.org/idp/metadata",
                "external_id": str(self.user.pk),
                "profile": {
                    "username": "testuser",
                    "last_name": "Weblate Test",
                    "email": "weblate@example.org",
                    "active": True,
                    "is_active": True,
                },
                "changes": {
                    "username": "testuser",
                    "last_name": "Weblate Test",
                    "email": "weblate@example.org",
                    "active": True,
                    "is_active": True,
                },
            },
        )
        self.assertEqual(
            get_user_sync_payload(self.user, {"is_active": False})["changes"],
            {"is_active": False, "active": False},
        )

    @override_settings(PAYMENT_SECRET=TEST_PAYMENT_SECRET)
    def test_queue_user_sync_refreshes_cursor(self) -> None:
        sync_state = UserSyncState.objects.get_or_create(user=self.user)[0]
        old_updated = timezone.now() - relativedelta(hours=1)
        UserSyncState.objects.filter(pk=sync_state.pk).update(updated=old_updated)

        with patch("wlhosted.integrations.tasks.notify_user_change.delay") as delay:
            queue_user_sync(self.user)

        sync_state.refresh_from_db()
        self.assertGreater(sync_state.updated, old_updated)
        delay.assert_called_once()

    @override_settings(PAYMENT_SECRET="")
    def test_queue_user_sync_no_payment_secret(self) -> None:
        UserSyncState.objects.filter(user=self.user).delete()

        with patch("wlhosted.integrations.tasks.notify_user_change.delay") as delay:
            queue_user_sync(self.user)

        self.assertFalse(UserSyncState.objects.filter(user=self.user).exists())
        delay.assert_not_called()

    @override_settings(PAYMENT_SECRET=TEST_PAYMENT_SECRET)
    @responses.activate
    def test_notify_user_change(self) -> None:
        responses.add(responses.POST, "https://weblate.org/api/user/", body="")
        notify_user_change(get_user_sync_payload(self.user))

        request = responses.calls[0].request
        request_body = request.body
        if isinstance(request_body, bytes):
            request_body = request_body.decode()
        if not isinstance(request_body, str):
            self.fail(f"Unexpected request body type: {type(request_body).__name__}")
        body = parse_qs(request_body)
        payload = loads(
            body["payload"][0], key=TEST_PAYMENT_SECRET, salt="weblate.user"
        )
        self.assertEqual(payload["external_id"], str(self.user.pk))
        self.assertEqual(payload["profile"]["email"], "weblate@example.org")

    @override_settings(PAYMENT_SECRET=TEST_PAYMENT_SECRET)
    def test_api_users(self) -> None:
        UserSyncState.objects.get_or_create(user=self.user)
        response = self.client.post(
            reverse("hosted-api-users"),
            {
                "payload": dumps(
                    {"since": ""},
                    key=TEST_PAYMENT_SECRET,
                    salt="weblate.user-sync",
                )
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["Cache-Control"])
        payload = loads(
            response.json()["payload"],
            key=TEST_PAYMENT_SECRET,
            salt="weblate.user-sync-response",
        )
        self.assertIn(
            str(self.user.pk), {user["external_id"] for user in payload["users"]}
        )

    @override_settings(PAYMENT_SECRET=TEST_PAYMENT_SECRET)
    def test_api_users_requires_post(self) -> None:
        response = self.client.get(
            reverse("hosted-api-users"),
            {
                "payload": dumps(
                    {"since": ""},
                    key=TEST_PAYMENT_SECRET,
                    salt="weblate.user-sync",
                )
            },
        )

        self.assertEqual(response.status_code, 405)

    @override_settings(PAYMENT_SECRET=TEST_PAYMENT_SECRET)
    def test_api_users_incremental(self) -> None:
        stale_user = self.user
        with patch("wlhosted.integrations.tasks.notify_user_change.delay"):
            changed_user = User.objects.create_user(
                username="changed",
                email="changed@example.org",
                password=TESTPASSWORD,
            )
        since = timezone.now() - relativedelta(minutes=30)
        changed_updated = timezone.now() - relativedelta(minutes=10)
        UserSyncState.objects.get_or_create(user=stale_user)
        UserSyncState.objects.filter(user=stale_user).update(
            updated=timezone.now() - relativedelta(hours=1)
        )
        UserSyncState.objects.get_or_create(user=changed_user)
        UserSyncState.objects.filter(user=changed_user).update(updated=changed_updated)

        response = self.client.post(
            reverse("hosted-api-users"),
            {
                "payload": dumps(
                    {"since": since.isoformat()},
                    key=TEST_PAYMENT_SECRET,
                    salt="weblate.user-sync",
                )
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = loads(
            response.json()["payload"],
            key=TEST_PAYMENT_SECRET,
            salt="weblate.user-sync-response",
        )
        self.assertEqual(
            [user["external_id"] for user in payload["users"]],
            [str(changed_user.pk)],
        )
        self.assertEqual(payload["cursor"], changed_updated.isoformat())

    @override_settings(PAYMENT_SECRET=TEST_PAYMENT_SECRET)
    def test_api_users_invalid_cursor(self) -> None:
        for since in ("invalid", 1, True, "2026-05-25T12:00:00"):
            with self.subTest(since=since):
                response = self.client.post(
                    reverse("hosted-api-users"),
                    {
                        "payload": dumps(
                            {"since": since},
                            key=TEST_PAYMENT_SECRET,
                            salt="weblate.user-sync",
                        )
                    },
                )

                self.assertEqual(response.status_code, 400)

    @override_settings(PAYMENT_SECRET="")
    def test_api_users_no_payment_secret(self) -> None:
        response = self.client.post(
            reverse("hosted-api-users"),
            {"payload": dumps({"since": ""}, key="", salt="weblate.user-sync")},
        )

        self.assertEqual(response.status_code, 400)

    def test_pending_payments(self) -> None:
        self.test_create()
        Payment.objects.all().update(state=Payment.ACCEPTED)
        pending_payments()
        self.assertFalse(Payment.objects.filter(state=Payment.ACCEPTED).exists())

    def test_existing_billing(self) -> None:
        bill = self.create_trial()
        bill.removal = timezone.now()
        bill.save(update_fields=["removal"])
        bill_args = {"billing": bill.pk}
        # Test default selection
        response = self.client.get(reverse("create-billing"))
        self.assertContains(response, "Trial")
        # Test manual selection
        response = self.client.get(reverse("create-billing"), bill_args)
        self.assertContains(response, "Trial")
        # Test invalid selection
        response = self.client.get(reverse("create-billing"), {"billing": "x"})
        self.assertNotContains(response, "Trial")
        # Create payment for billing
        self.create_payment(**bill_args)
        payment = Payment.objects.all()[0]
        bill_args["plan"] = self.plan_a.pk
        bill_args["period"] = "y"
        # The billing should be stored in the payment
        self.assertEqual(payment.extra, bill_args)

        # Accept the payment
        Payment.objects.all().update(state=Payment.ACCEPTED)
        pending_payments()
        # User should get notification that project scheduled for removal is now paid
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "[Weblate] Your billing plan was paid")

    def test_error_handling(self) -> None:
        response = self.client.post(reverse("create-billing"))
        self.assertContains(response, "This field is required")

        with override_settings(PAYMENT_ENABLED=False):
            response = self.client.post(
                reverse("create-billing"), {"plan": self.plan_a.id, "period": "y"}
            )
            self.assertRedirects(response, reverse("create-billing"))

    @override_settings(PAYMENT_REDIRECT_URL="http://example.com/payment")
    def test_payment_redirects(self) -> None:
        # Invalid UUID
        self.assertRedirects(
            self.client.get(reverse("create-billing"), {"payment": "i"}),
            reverse("create-billing"),
        )
        self.create_payment()
        payment = Payment.objects.all()[0]
        bill_url = reverse("billing")
        create_url = reverse("create-billing")
        pay_url = "http://example.com/payment"
        pay_params = {"payment": str(payment.uuid)}
        # New should redirect to payment interface
        self.assertRedirects(
            self.client.get(create_url, pay_params),
            pay_url,
            fetch_redirect_response=False,
        )
        # Pending should redirect to billings
        payment.state = Payment.PENDING
        payment.save()
        self.assertRedirects(self.client.get(create_url, pay_params), bill_url)
        # Accepted should redirect to billings
        payment.state = Payment.ACCEPTED
        payment.save()
        response = self.client.get(create_url, pay_params, follow=True)
        bill_url = Billing.objects.get().get_absolute_url()
        self.assertRedirects(response, bill_url)
        # Processed should redirect to billings
        payment.state = Payment.PROCESSED
        payment.save()
        self.assertRedirects(
            self.client.get(create_url, pay_params, follow=True),
            bill_url,
        )
        # Rejected should redirect to create
        payment.state = Payment.REJECTED
        payment.save()
        self.assertRedirects(self.client.get(create_url, pay_params), create_url)
        # Non existing should redirect to create
        payment.delete()
        self.assertRedirects(self.client.get(create_url, pay_params), create_url)

    def do_complete(self, customer_name: str = "", **kwargs):
        self.create_payment(**kwargs)
        payment = Payment.objects.all()[0]
        if customer_name:
            Customer.objects.update(name=customer_name)
        payment.state = Payment.ACCEPTED
        payment.save()
        response = self.client.get(
            reverse("create-billing"), {"payment": str(payment.uuid)}, follow=True
        )
        if "billing" in kwargs:
            billing = Billing.objects.get(pk=kwargs["billing"])
        else:
            billing = Billing.objects.all()[0]
        self.assertRedirects(response, billing.get_absolute_url())
        return billing

    def test_complete(self) -> None:
        bill = self.do_complete()
        self.assertEqual(bill.state, Billing.STATE_ACTIVE)
        self.assertEqual(bill.plan, self.plan_a)

    def test_complete_customer_name(self) -> None:
        bill = self.do_complete(customer_name="Acme Billing LLC")
        self.assertEqual(bill.customer_name, "Acme Billing LLC")

    def test_complete_monthly(self) -> None:
        self.do_complete(period="m")
        bill = Billing.objects.all()[0]
        self.assertEqual(bill.state, Billing.STATE_ACTIVE)
        self.assertEqual(bill.plan, self.plan_a)

    def test_complete_trial(self) -> None:
        bill = self.create_trial()
        bill = self.do_complete(billing=bill.pk)
        self.assertEqual(bill.state, Billing.STATE_ACTIVE)
        self.assertEqual(bill.plan, self.plan_a)

    def test_complete_updates_customer_name(self) -> None:
        bill = self.create_trial()
        bill = self.do_complete(billing=bill.pk, customer_name="Updated Customer")
        self.assertEqual(bill.customer_name, "Updated Customer")

    def test_complete_second(self) -> None:
        bill = self.create_trial()
        now = timezone.now()
        Invoice.objects.create(
            billing=bill, start=now, end=now + relativedelta(months=1), amount=10
        )
        old_i = bill.invoice_set.all()[0]
        bill = self.do_complete(billing=bill.pk)
        self.assertEqual(bill.state, Billing.STATE_ACTIVE)
        self.assertEqual(bill.plan, self.plan_a)
        self.assertEqual(bill.invoice_set.count(), 2)
        new_i = bill.invoice_set.exclude(pk=old_i.pk)[0]
        self.assertLess(old_i.end, new_i.start)

    def prepare_recurring(self, method):
        self.create_payment(period="y")
        payment = Payment.objects.all()[0]

        # Complete the payment
        backend = get_backend(method)(payment)
        backend.initiate(None, "", "")
        Customer.objects.update(
            name="Michal Čihař",
            address="Zdiměřická 1439",
            city="149 00 Praha 4",
            country="CZ",
            vat="CZ8003280318",
        )
        backend.complete(None)

        response = self.client.get(
            reverse("create-billing"), {"payment": str(payment.uuid)}, follow=True
        )
        billing = Billing.objects.all()[0]
        self.assertRedirects(response, billing.get_absolute_url())

        # Check recurrence is stored
        bill = Billing.objects.all()[0]
        invoices = bill.invoice_set.count()

        # Fake end of last invoice
        last_invoice = bill.invoice_set.order_by("-start")[0]
        last_invoice.end = timezone.now() - relativedelta(days=7)
        last_invoice.save()

        return payment, bill, invoices

    def run_recurring(self, *, add_project: bool = True, add_user: bool = True) -> None:
        # Make sure billing has a project
        bill = Billing.objects.get()
        project = Project.objects.create(name="Project", slug="project")
        if add_project:
            bill.projects.add(project)
        if add_user:
            project.add_user(self.user)
        # Invoke recurring payment
        responses.add(responses.POST, "http://example.com/payment", body="")
        recurring_payments()

    @override_settings(
        PAYMENT_DEBUG=True, PAYMENT_REDIRECT_URL="http://example.com/payment"
    )
    @responses.activate
    def test_recurring(self) -> None:
        """Test recurring payments."""
        payment, bill, invoices = self.prepare_recurring("pay")
        self.assertEqual(bill.payment["recurring"], str(payment.pk))
        self.assertEqual(bill.customer_name, "Michal Čihař")

        self.run_recurring()

        # Complete the payment (we've faked the payment server above)
        recure_payment = Payment.objects.exclude(pk=payment.pk)[0]
        backend = get_backend("pay")(recure_payment)
        backend.initiate(None, "", "")
        backend.complete(None)

        # Process pending payments
        pending_payments()

        # There should be additional invoice on the billing
        self.assertEqual(invoices + 1, bill.invoice_set.count())

    @override_settings(
        PAYMENT_DEBUG=True, PAYMENT_REDIRECT_URL="http://example.com/payment"
    )
    @responses.activate
    def test_recurring_updates_customer_name(self) -> None:
        """Test recurring payments update customer name."""
        payment, bill, _invoices = self.prepare_recurring("pay")
        Customer.objects.update(name="Updated Recurring Customer")

        self.run_recurring()

        recure_payment = Payment.objects.exclude(pk=payment.pk)[0]
        backend = get_backend("pay")(recure_payment)
        backend.initiate(None, "", "")
        backend.complete(None)
        pending_payments()

        bill.refresh_from_db()
        self.assertEqual(bill.customer_name, "Updated Recurring Customer")

    @override_settings(PAYMENT_DEBUG=True)
    def test_recurring_none(self) -> None:
        """Test method without support for recurring payments."""
        # The pending method does not support recurring payments
        payment, bill, _invoices = self.prepare_recurring("pending")
        self.assertNotIn("recurring", bill.payment)

        self.run_recurring()

        # There should be no new payment
        self.assertFalse(Payment.objects.exclude(pk=payment.pk).exists())

    @override_settings(PAYMENT_DEBUG=True)
    def test_recurring_invalid(self) -> None:
        """Test handling of invalid (removed) method."""
        payment, bill, _invoices = self.prepare_recurring("pay")
        self.assertEqual(bill.payment["recurring"], str(payment.pk))

        # Fake payment method
        payment.details["backend"] = "invalid"
        payment.save()

        self.run_recurring()

        # There should be no new payment
        self.assertFalse(Payment.objects.exclude(pk=payment.pk).exists())
        # Recurrence should be disabled
        bill = Billing.objects.get(pk=bill.pk)
        self.assertNotIn("recurring", bill.payment)

    @override_settings(PAYMENT_DEBUG=True)
    def test_recurring_no_project(self) -> None:
        """Test handling of invalid (removed) method."""
        payment, bill, _invoices = self.prepare_recurring("pay")
        self.assertEqual(bill.payment["recurring"], str(payment.pk))

        self.run_recurring(add_project=False)

        # There should be no new payment
        self.assertFalse(Payment.objects.exclude(pk=payment.pk).exists())

    @override_settings(PAYMENT_DEBUG=True)
    def test_recurring_no_users(self) -> None:
        """Test handling of invalid (removed) method."""
        payment, bill, _invoices = self.prepare_recurring("pay")
        self.assertEqual(bill.payment["recurring"], str(payment.pk))

        self.run_recurring(add_user=False)

        # There should be no new payment
        self.assertFalse(Payment.objects.exclude(pk=payment.pk).exists())

    @override_settings(
        PAYMENT_DEBUG=True, PAYMENT_REDIRECT_URL="http://example.com/payment"
    )
    @responses.activate
    def test_recurring_one_error(self) -> None:
        """Test handling of single failed recurring payments."""
        payment, bill, invoices = self.prepare_recurring("pay")
        self.assertEqual(bill.payment["recurring"], str(payment.pk))

        Payment.objects.create(
            repeat=payment, customer=payment.customer, state=Payment.REJECTED, amount=1
        )

        self.run_recurring()

        # Complete the payment (we've faked the payment server above)
        recure_payment = Payment.objects.exclude(pk=payment.pk).exclude(amount=1)[0]
        backend = get_backend("pay")(recure_payment)
        backend.initiate(None, "", "")
        backend.complete(None)

        # Process pending payments
        pending_payments()

        # There should be additional invoice on the billing
        self.assertEqual(invoices + 1, bill.invoice_set.count())

    @override_settings(PAYMENT_DEBUG=True)
    def test_recurring_more_error(self) -> None:
        """Test handling of more failed recurring payments."""
        payment, bill, _invoices = self.prepare_recurring("pay")
        self.assertEqual(bill.payment["recurring"], str(payment.pk))

        Payment.objects.create(
            repeat=payment, customer=payment.customer, state=Payment.PROCESSED, amount=1
        )
        # Ensure rest is after processed one
        sleep(1)
        Payment.objects.create(
            repeat=payment, customer=payment.customer, state=Payment.REJECTED, amount=1
        )
        Payment.objects.create(
            repeat=payment, customer=payment.customer, state=Payment.REJECTED, amount=1
        )
        Payment.objects.create(
            repeat=payment, customer=payment.customer, state=Payment.REJECTED, amount=1
        )

        self.run_recurring()

        # There should be no new payment
        self.assertFalse(
            Payment.objects.exclude(pk=payment.pk).exclude(amount=1).exists()
        )
        # Recurrence should be disabled
        bill = Billing.objects.get(pk=bill.pk)
        self.assertNotIn("recurring", bill.payment)
