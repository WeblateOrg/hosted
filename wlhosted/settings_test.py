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

import os.path

from weblate.settings_test import *  # noqa: F403

INSTALLED_APPS += (
    "wlhosted",
    "wlhosted.payments",
    "wlhosted.integrations",
)

DATABASES["payments_db"] = {
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": os.path.join(DATA_DIR, "payments.db"),
}
DATABASES["memory_db"] = DATABASES["default"]

DATABASE_ROUTERS = ["wlhosted.dbrouter.HostedRouter"]

LOCALE_PATHS = [os.path.join(os.path.dirname(__file__), "locale")]

FIO_TOKEN = "test-token"
