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

import re
from functools import partial
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import BadRequest, ValidationError
from django.core.signing import BadSignature, SignatureExpired, dumps, loads
from django.db import IntegrityError, transaction
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.views.generic.edit import FormView
from weblate.accounts.flows import PASSWORD_RESET_SCOPE_WEBLATE_SERVICES
from weblate.accounts.models import EXTERNAL_CREATE_ACTIVITY, AuditLog
from weblate.accounts.views import send_password_reset_email
from weblate.auth.models import User
from weblate.billing.models import Billing, Plan
from weblate.utils import messages
from weblate.utils.views import show_form_errors

from wlhosted.integrations.forms import (
    BillingForm,
    ChooseBillingForm,
    EnsureHostedUserForm,
)
from wlhosted.integrations.models import (
    UserSyncState,
    get_user_sync_payload,
    handle_received_payment,
)
from wlhosted.integrations.utils import get_origin
from wlhosted.payments.models import Payment

if TYPE_CHECKING:
    from weblate.auth.models import AuthenticatedHttpRequest


USER_SYNC_SALT = "weblate.user-sync"
USER_SYNC_RESPONSE_SALT = "weblate.user-sync-response"
USER_ENSURE_SALT = "weblate.user-ensure"
USER_ENSURE_RESPONSE_SALT = "weblate.user-ensure-response"
USERNAME_ALLOWED_RE = re.compile(r"[^\w.@+-]+")


def get_default_billing(user):
    """Get trial billing for user to be upgraded."""
    billings = Billing.objects.for_user(user).filter(state=Billing.STATE_TRIAL)
    if billings.count() == 1:
        return billings[0]
    return None


def get_username_max_length() -> int:
    max_length = User._meta.get_field("username").max_length  # pylint: disable=protected-access
    return int(max_length or 150)


def normalize_username(username: str) -> str:
    result = USERNAME_ALLOWED_RE.sub("-", username.strip())
    return (result or "weblate-user")[: get_username_max_length()]


def make_unique_username(email: str) -> str:
    username = normalize_username(email.strip().casefold().rsplit("@", 1)[0])
    if not User.objects.filter(username__iexact=username).exists():
        return username
    max_length = get_username_max_length()
    counter = 1
    while True:
        suffix = f"-{counter}"
        candidate = f"{username[: max_length - len(suffix)]}{suffix}"
        if not User.objects.filter(username__iexact=candidate).exists():
            return candidate
        counter += 1


def get_hosted_user_by_email(email: str) -> User | None:
    users = list(User.objects.filter(email__iexact=email).order_by("pk")[:2])
    if len(users) > 1:
        raise BadRequest("Multiple hosted users use this e-mail address")
    if users:
        return users[0]
    return None


def ensure_hosted_user(request, email: str, full_name: str) -> tuple[User, bool]:
    if user := get_hosted_user_by_email(email):
        return user, False

    for attempt in range(2):
        try:
            with transaction.atomic():
                if user := get_hosted_user_by_email(email):
                    return user, False
                username = make_unique_username(email)
                if user := get_hosted_user_by_email(email):
                    return user, False
                user = User.objects.create_user(
                    username=username,
                    email=email,
                    full_name=full_name,
                )
                AuditLog.objects.create(user, None, EXTERNAL_CREATE_ACTIVITY)
                transaction.on_commit(
                    partial(
                        send_password_reset_email,
                        request,
                        user,
                        email=email,
                        scope=PASSWORD_RESET_SCOPE_WEBLATE_SERVICES,
                    )
                )
                return user, True
        except IntegrityError:
            if user := get_hosted_user_by_email(email):
                return user, False
            if attempt:
                raise

    raise BadRequest("Could not create hosted user")


def get_signed_payload(request, salt: str) -> dict:
    if not settings.PAYMENT_SECRET:
        raise BadRequest("Integrations API is disabled")

    try:
        payload = loads(
            request.POST.get("payload", ""),
            key=settings.PAYMENT_SECRET,
            max_age=300,
            salt=salt,
        )
    except (BadSignature, SignatureExpired) as error:
        raise BadRequest("Invalid signature") from error
    if not isinstance(payload, dict):
        raise BadRequest("Invalid payload")
    return payload


@csrf_exempt
@require_POST
@never_cache
def api_users(request):
    payload = get_signed_payload(request, USER_SYNC_SALT)

    since = payload.get("since")
    if since not in (None, ""):
        if not isinstance(since, str):
            raise BadRequest("Invalid cursor")
        since_dt = parse_datetime(since)
        if since_dt is None or timezone.is_naive(since_dt):
            raise BadRequest("Invalid cursor")
        now = timezone.now()
        if since_dt > now:
            raise BadRequest("Invalid cursor")
        sync_states = list(
            UserSyncState.objects.filter(updated__gt=since_dt)
            .select_related("user")
            .order_by("user_id")
        )
        users = [sync_state.user for sync_state in sync_states]
        cursor = max(
            (sync_state.updated for sync_state in sync_states),
            default=since_dt,
        )
    else:
        cursor = timezone.now()
        users = list(User.objects.order_by("pk"))

    response_payload = {
        "cursor": cursor.isoformat(),
        "users": [get_user_sync_payload(user) for user in users],
    }
    return JsonResponse(
        {
            "payload": dumps(
                response_payload,
                key=settings.PAYMENT_SECRET,
                salt=USER_SYNC_RESPONSE_SALT,
            )
        }
    )


@csrf_exempt
@require_POST
@never_cache
def api_user_ensure(request):
    payload = get_signed_payload(request, USER_ENSURE_SALT)
    form = EnsureHostedUserForm(payload)
    if not form.is_valid():
        raise BadRequest("Invalid user payload")

    user, created = ensure_hosted_user(
        request,
        form.cleaned_data["email"],
        form.cleaned_data["full_name"],
    )
    response_payload = {
        "created": created,
        "user": get_user_sync_payload(user),
    }
    return JsonResponse(
        {
            "payload": dumps(
                response_payload,
                key=settings.PAYMENT_SECRET,
                salt=USER_ENSURE_RESPONSE_SALT,
            )
        }
    )


@method_decorator(login_required, name="dispatch")
class CreateBillingView(FormView):
    template_name = "hosted/create.html"
    form_class = BillingForm
    request: AuthenticatedHttpRequest

    def get_form_kwargs(self):
        result = super().get_form_kwargs()
        if "do" in self.request.GET:
            result["data"] = self.request.GET
        result["user"] = self.request.user
        return result

    def handle_payment(self, request):
        try:
            payment = Payment.objects.select_for_update().get(
                uuid=request.GET["payment"],
                customer__user_id=request.user.id,
                customer__origin=get_origin(),
            )
        except (Payment.DoesNotExist, ValidationError):
            messages.error(request, _("No matching payment found."))
            return redirect("create-billing")

        if payment.state in (Payment.ACCEPTED, Payment.PROCESSED):
            if payment.state == Payment.ACCEPTED:
                handle_received_payment(payment)

            messages.success(
                request, _("Thank you for purchasing a hosting plan, it is now active.")
            )
            return redirect("billing")

        if payment.state in (Payment.PENDING, Payment.PROCESSED):
            messages.info(
                request,
                _(
                    "Thank you for purchasing a hosting plan, the payment for it is "
                    "pending and will be processed in the background."
                ),
            )
            return redirect("billing")

        if payment.state == Payment.NEW:
            return HttpResponseRedirect(payment.get_payment_url())

        if payment.state == Payment.REJECTED:
            messages.error(
                request,
                _("The payment was rejected: {}").format(
                    payment.details.get("reject_reason", _("Unknown reason"))
                ),
            )

        return redirect("create-billing")

    def get(self, request, *args, **kwargs):
        if "do" in request.GET:
            return self.post(request, *args, **kwargs)
        if "payment" in request.GET:
            with transaction.atomic(using="payments_db"):
                return self.handle_payment(request)
        return super().get(request, *args, **kwargs)

    def form_valid(self, form):
        if not settings.PAYMENT_ENABLED:
            messages.error(self.request, _("Payments are temporarily inactive."))
            return redirect("create-billing")
        with transaction.atomic(using="payments_db"):
            payment = form.create_payment(self.request.user)
            return HttpResponseRedirect(payment.get_payment_url())

    def form_invalid(self, form):
        show_form_errors(self.request, form)
        return super().form_invalid(form)

    def get_context_data(self, **kwargs):
        kwargs = super().get_context_data(**kwargs)
        kwargs["plans"] = list(Plan.objects.public(self.request.user))
        default_billing = get_default_billing(self.request.user)
        has_billing = Billing.objects.for_user(self.request.user).exists()
        if "billing" in self.request.GET or "plan" in self.request.GET:
            data = self.request.GET
        else:
            data = None
        form = ChooseBillingForm(self.request.user, data)
        kwargs["selected_plan"] = None
        if form.is_valid():
            kwargs["billing"] = form.cleaned_data["billing"]
            kwargs["selected_plan"] = form.cleaned_data["plan"]
        elif data is None:
            kwargs["billing"] = default_billing
        else:
            kwargs["billing"] = None
        # Show billing selection if needed (hide for upgrades and
        # when user has no billing plan)
        if has_billing and "upgrade" not in self.request.GET:
            kwargs["choose_billing"] = form
        if kwargs["billing"]:
            for plan in kwargs["plans"]:
                plan.would_fit = kwargs["billing"].in_display_limits(plan)
        return kwargs
