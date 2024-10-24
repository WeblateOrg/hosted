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
from django.utils import timezone

import datetime
from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from weblate.billing.models import Billing, Plan

from wlhosted.integrations.utils import get_origin
from wlhosted.payments.models import Customer, Payment, get_period_delta, date_format


class ChooseBillingForm(forms.Form):
    billing = forms.ModelChoiceField(
        queryset=Billing.objects.none(),
        label=_("Billing"),
        help_text=_("Choose the billing plan you want to update"),
        empty_label=_("Create new billing plan"),
        required=False,
    )
    plan = forms.ModelChoiceField(
        queryset=Plan.objects.public(), widget=forms.HiddenInput, required=False
    )

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["billing"].queryset = Billing.objects.for_user(user)


class BillingForm(ChooseBillingForm):
    plan = forms.ModelChoiceField(
        queryset=Plan.objects.public(), widget=forms.HiddenInput
    )
    period = forms.ChoiceField(
        choices=[("y", "y"), ("m", "m")], widget=forms.HiddenInput
    )
    extra_domain = forms.BooleanField(required=False, widget=forms.HiddenInput)

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        self.fields["plan"].queryset = Plan.objects.public(user)

    def clean(self):
        plan = self.cleaned_data.get("plan")
        period = self.cleaned_data.get("period")
        if not plan or not period:
            return
        if not plan.yearly_price and period == "y":
            raise ValidationError("Plan does not support yearly billing!")
        if not plan.price and period == "m":
            raise ValidationError("Plan does not support monthly billing!")

    def create_payment(self, user):
        customer = Customer.objects.get_or_create(
            origin=get_origin(), user_id=user.id, defaults={"email": user.email}
        )[0]

        plan = self.cleaned_data["plan"]
        period = self.cleaned_data["period"]
        extra = {
            "plan": plan.pk,
            "period": period,
        }
        if billing := self.cleaned_data["billing"]:
            extra["billing"] = billing.pk
            try:
                invoice = billing.get_last_invoice_object()
            except IndexError:
                start_date = timezone.now()
            else:
                start_date = invoice.end + datetime.timedelta(days=1)
        else:
            start_date = timezone.now()
        end_date = start_date + get_period_delta(period)

        description = f"Weblate hosting ({plan.name}) [{date_format(start_date)} - {date_format(end_date)}]"
        amount = plan.price if period == "m" else plan.yearly_price
        if self.cleaned_data["extra_domain"]:
            amount += 100
            description += " + Custom domain"
        return Payment.objects.create(
            amount=amount,
            description=description,
            recurring=self.cleaned_data["period"],
            customer=customer,
            extra=extra,
        )
