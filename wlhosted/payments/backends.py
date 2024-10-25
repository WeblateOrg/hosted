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

import os
import re

from django.conf import settings
from django.core.mail import EmailMessage
from django.shortcuts import redirect
from django.utils.translation import gettext, gettext_lazy

from wlhosted.payments.models import Payment

BACKENDS = {}
PROFORMA_RE = re.compile("20[0-9]{7}")


def get_backend(name):
    backend = BACKENDS[name]
    if backend.debug and not settings.PAYMENT_DEBUG:
        raise KeyError("Invalid backend")
    return backend


def list_backends():
    result = []
    for backend in BACKENDS.values():
        if not backend.debug or settings.PAYMENT_DEBUG:
            result.append(backend)
    return sorted(result, key=lambda x: x.name)


class InvalidState(ValueError):
    pass


def register_backend(backend):
    BACKENDS[backend.name] = backend
    return backend


class Backend:
    name = None
    debug = False
    verbose = None
    description = ""
    recurring = False

    def __init__(self, payment):
        select = Payment.objects.filter(pk=payment.pk).select_for_update()
        self.payment = select[0]
        self.invoice = None

    @property
    def image_name(self):
        return f"payment/{self.name}.png"

    def perform(self, request, back_url, complete_url):
        """Performs payment and optionally redirects user."""
        raise NotImplementedError

    def collect(self, request):
        """Collects payment information."""
        raise NotImplementedError

    def get_instructions(self):
        """Payment instructions for manual methods."""
        return []

    def initiate(self, request, back_url, complete_url):
        """Initiates payment and optionally redirects user."""
        if self.payment.state != Payment.NEW:
            raise InvalidState

        if self.payment.repeat and not self.recurring:
            raise InvalidState

        result = self.perform(request, back_url, complete_url)

        # Update payment state
        self.payment.state = Payment.PENDING
        self.payment.backend = self.name
        self.payment.save()

        return result

    def complete(self, request):
        """Payment completion called from returned request."""
        if self.payment.state != Payment.PENDING:
            raise InvalidState

        status = self.collect(request)
        if status is None:
            return False
        if status:
            self.success()
            return True
        self.failure()
        return False

    def notify_user(self):
        """Send email notification with an invoice."""
        email = EmailMessage(
            gettext("Your payment on weblate.org"),
            gettext(
                """Hello,

Thank you for your payment on weblate.org.

You will find an invoice for this payment attached.
Alternatively, you can download it from the website:

%s
"""
            )
            % self.payment.customer.origin,
            "billing@weblate.org",
            [self.payment.customer.email],
        )
        if self.invoice is not None:
            with open(self.invoice.pdf_path, "rb") as handle:
                email.attach(
                    os.path.basename(self.invoice.pdf_path),
                    handle.read(),
                    "application/pdf",
                )
        email.send()

    def notify_failure(self):
        """Send email notification with a failure."""
        email = EmailMessage(
            gettext("Your payment on weblate.org failed"),
            gettext(
                """Hello,

Your payment on weblate.org has failed.

%s

Retry issuing the payment on the website:

%s

If concerning a recurring payment, it is retried three times,
and if still failing, cancelled.
"""
            )
            % (
                self.payment.details.get("reject_reason", "Uknown"),
                self.payment.customer.origin,
            ),
            "billing@weblate.org",
            [self.payment.customer.email],
        )
        if self.invoice is not None:
            with open(self.invoice.pdf_path, "rb") as handle:
                email.attach(
                    os.path.basename(self.invoice.pdf_path),
                    handle.read(),
                    "application/pdf",
                )
        email.send()

    def notify_pending(self):
        """Send email notification with a pending."""
        email = EmailMessage(
            gettext("Your pending payment on weblate.org"),
            gettext(
                """Hello,

Your payment on weblate.org is pending. Please follow the provided
instructions to complete the payment.
"""
            ),
            "billing@weblate.org",
            [self.payment.customer.email],
        )
        if self.invoice is not None:
            with open(self.invoice.pdf_path, "rb") as handle:
                email.attach(
                    os.path.basename(self.invoice.pdf_path),
                    handle.read(),
                    "application/pdf",
                )
        email.send()

    def get_invoice_kwargs(self):
        return {"payment_id": str(self.payment.pk), "payment_method": self.description}

    def success(self):
        self.payment.state = Payment.ACCEPTED
        if not self.recurring:
            self.payment.recurring = ""

        self.payment.save()

        self.notify_user()

    def failure(self):
        self.payment.state = Payment.REJECTED
        self.payment.save()

        self.notify_failure()


@register_backend
class DebugPay(Backend):
    name = "pay"
    debug = True
    verbose = "Pay"
    description = "Paid (TEST)"
    recurring = True

    def perform(self, request, back_url, complete_url):
        return None

    def collect(self, request):
        return True


@register_backend
class DebugReject(DebugPay):
    name = "reject"
    verbose = "Reject"
    description = "Reject (TEST)"
    recurring = False

    def collect(self, request):
        self.payment.details["reject_reason"] = "Debug reject"
        return False


@register_backend
class DebugPending(DebugPay):
    name = "pending"
    verbose = "Pending"
    description = "Pending (TEST)"
    recurring = False

    def perform(self, request, back_url, complete_url):
        return redirect("https://cihar.com/?url=" + complete_url)

    def collect(self, request):
        return True


@register_backend
class FioBank(Backend):
    name = "fio-bank"
    verbose = gettext_lazy("IBAN bank transfer")
    description = "Bank transfer"
    recurring = False


@register_backend
class ThePay2Card(Backend):
    name = "thepay2-card"
    verbose = gettext_lazy("Payment card")
    description = "Payment Card (The Pay)"
    recurring = True
