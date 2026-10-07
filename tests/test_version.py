"""Tests for `git-curate --version`."""

from __future__ import annotations

import zipfile
from importlib.metadata import version
from pathlib import Path

import sh
from typer.testing import CliRunner

from git_curate.cli import app

_REPO = Path(__file__).parent.parent
_runner = CliRunner()


def _describe() -> str:
    return str(sh.git("describe", "--always", "--dirty", "--exclude=*", _cwd=_REPO)).strip()


def test_version_prints_version_and_commit() -> None:
    result = _runner.invoke(app, ["--version"])

    assert result.exit_code == 0, result.output
    assert result.output == f"git-curate {version('git-curate')} ({_describe()})\n"


def test_wheel_records_build_commit(tmp_path: Path) -> None:
    sh.uv("build", "--wheel", "--out-dir", tmp_path, _cwd=_REPO)

    (wheel,) = tmp_path.glob("*.whl")
    with zipfile.ZipFile(wheel) as zf:
        build_info = zf.read("git_curate/_build_info.py").decode()
    assert build_info == f"commit = {_describe()!r}\n"
