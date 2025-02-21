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

import re
from typing import Never

from django.conf import settings
from django.shortcuts import redirect
from django.utils.translation import gettext_lazy

from wlhosted.payments.models import Payment

BACKENDS = {}
PROFORMA_RE = re.compile("20[0-9]{7}")


def get_backend(name):
    backend = BACKENDS[name]
    if backend.debug and not settings.PAYMENT_DEBUG:
        raise KeyError("Invalid backend")
    return backend


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

    def perform(self, request, back_url, complete_url) -> Never:
        """Performs payment and optionally redirects user."""
        raise NotImplementedError

    def collect(self, request) -> Never:
        """Collects payment information."""
        raise NotImplementedError

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

    def complete(self, request) -> bool:
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

    def success(self) -> None:
        self.payment.state = Payment.ACCEPTED
        if not self.recurring:
            self.payment.recurring = ""

        self.payment.save()

    def failure(self) -> None:
        self.payment.state = Payment.REJECTED
        self.payment.save()


@register_backend
class DebugPay(Backend):
    name = "pay"
    debug = True
    verbose = "Pay"
    description = "Paid (TEST)"
    recurring = True

    def perform(self, request, back_url, complete_url) -> None:
        return None

    def collect(self, request) -> bool:
        return True


@register_backend
class DebugReject(DebugPay):
    name = "reject"
    verbose = "Reject"
    description = "Reject (TEST)"
    recurring = False

    def collect(self, request) -> bool:
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

    def collect(self, request) -> bool:
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
