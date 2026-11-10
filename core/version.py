"""The app version: the VERSION file in the checkout."""
from core import paths


def current() -> str:
    try:
        return (paths.CODE / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"
