"""The version must be single-sourced from pyproject.toml."""

import tomllib
from pathlib import Path

import stunt

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    with (REPO_ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)["project"]["version"]


def test_version_matches_pyproject():
    assert stunt.__version__ == _pyproject_version()


def test_addon_docstring_has_no_stale_version():
    docstring = REPO_ROOT.joinpath("src", "stunt", "addon.py").read_text().split('"""')[1]
    assert _pyproject_version() not in docstring
    # No other hardcoded "vX.Y.Z" string should sneak back into the docstring.
    import re

    assert not re.search(r"\bv\d+\.\d+\.\d+\b", docstring)
