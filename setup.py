#!/usr/bin/env python

# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
from distutils.command.build import build
from distutils.core import Command
from distutils.dep_util import newer
from glob import glob
from itertools import chain
from typing import ClassVar

from setuptools import setup
from translate.tools.pocompile import convertmo

LOCALE_MASKS = [
    "wlhosted/locale/*/LC_MESSAGES/*.po",
]


class BuildMo(Command):
    description = "update MO files to match PO"
    user_options: ClassVar = []

    def initialize_options(self) -> None:
        self.build_base = None

    def finalize_options(self) -> None:
        self.set_undefined_options("build", ("build_base", "build_base"))

    def run(self) -> None:
        for name in chain.from_iterable(glob(mask) for mask in LOCALE_MASKS):
            output = os.path.splitext(name)[0] + ".mo"
            if not newer(name, output):
                continue
            print(f"compiling {name} -> {output}")
            with open(name, "rb") as pofile, open(output, "wb") as mofile:
                convertmo(pofile, mofile, None)


class WeblateBuild(build):
    """Override the default build with new subcommands."""

    # The build_mo has to be before build_data
    sub_commands: ClassVar = [
        ("build_mo", lambda self: True),  # noqa: ARG005
        *build.sub_commands,
    ]


setup(
    cmdclass={"build_mo": BuildMo, "build": WeblateBuild},
)
