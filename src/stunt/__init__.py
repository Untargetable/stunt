"""
stunt — mitmproxy addon
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version
from pathlib import Path


def _read_version() -> str:
    # Source checkout or editable install: read pyproject.toml directly, so
    # __version__ reflects the running code rather than a stale dist-info.
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if pyproject.is_file():
        import tomllib

        with pyproject.open("rb") as f:
            data = tomllib.load(f)
        try:
            return data["project"]["version"]
        except KeyError:
            pass
    # Real install: pyproject.toml is not in the wheel, so use build-time metadata.
    try:
        return _installed_version("stunt")
    except PackageNotFoundError:
        return "0+unknown"


__version__ = _read_version()

from .addon import CompiledRule, Stunt, addons, load

__all__ = ["Stunt", "CompiledRule", "load", "addons"]
