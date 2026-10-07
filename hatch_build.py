"""Hatch build hook that records the source git commit in the wheel.

`uv tool install /path/to/git-curate` copies the package out of the checkout,
so `git-curate --version` can't ask git which commit it was built from. This
hook bakes the commit into `git_curate/_build_info.py` at build time.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import sh
from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class BuildInfoHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        # Editable installs run from the checkout and ask git at runtime instead.
        if version == "editable":
            return
        try:
            commit = str(sh.git("describe", "--always", "--dirty", "--exclude=*", _cwd=self.root)).strip()
        except (sh.ErrorReturnCode, sh.CommandNotFound):
            # Building from an sdist or without git: --version reports the commit as unknown.
            return
        self._tmpdir = tempfile.TemporaryDirectory()
        path = Path(self._tmpdir.name) / "_build_info.py"
        path.write_text(f"commit = {commit!r}\n")
        build_data["force_include"][str(path)] = "git_curate/_build_info.py"

    def finalize(self, version: str, build_data: dict[str, Any], artifact_path: str) -> None:
        if hasattr(self, "_tmpdir"):
            self._tmpdir.cleanup()
