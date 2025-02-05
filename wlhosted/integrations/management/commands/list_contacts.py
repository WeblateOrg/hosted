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
from weblate.auth.models import User
from weblate.billing.models import Billing


class Command(BaseCommand):
    help = "lists payments using obsolete payment method"

    def handle(self, *args, **options) -> None:
        emails = set()
        for billing in (
            Billing.objects.filter(state=Billing.STATE_ACTIVE)
            .exclude(plan__price=0)
            .prefetch()
        ):
            for user in billing.owners.all():
                emails.add(user.email)
            for project in billing.ordered_projects:
                for user in User.objects.having_perm("billing.manage", project):
                    emails.add(user.email)

        emails.discard("")

        for email in sorted(emails):
            self.stdout.write(email)
