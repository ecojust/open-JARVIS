"""Resource and user-data locations shared by source and frozen applications.

This module intentionally imports no Qt, audio or agent libraries.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


def resource_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def asset_path(relative: str) -> Path:
    return resource_root() / relative


def _absolute(value: str, base: Path | None = None) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else (base or resource_root()) / path).resolve()


def user_data_dir() -> Path:
    override = os.getenv("JARVIS_DATA_DIR", "").strip()
    if override:
        return _absolute(override)
    if sys.platform == "win32":
        return (
            Path(os.getenv("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
            / "OpenJARVIS"
        )
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "OpenJARVIS"
    return (
        Path(os.getenv("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
        / "open-jarvis"
    )


def preferences_path() -> Path:
    override = os.getenv("JARVIS_CONFIG_FILE", "").strip()
    return _absolute(override) if override else user_data_dir() / "settings.json"


def load_environment() -> None:
    """Precedence: process environment > saved preferences > source .env.

    Desktop save_preferences updates its environment before launching children,
    so a preference edited in the running app is inherited consistently.
    """
    from dotenv import dotenv_values

    defaults = {}
    if not getattr(sys, "frozen", False):
        defaults = dotenv_values(resource_root() / ".env")
        # Locate the saved configuration before applying the rest of .env.
        # Blank API keys in a copied .env.example must not shadow a key saved
        # from the desktop settings dialog on the next launch.
        for key in ("JARVIS_DATA_DIR", "JARVIS_CONFIG_FILE"):
            if defaults.get(key):
                os.environ.setdefault(key, defaults[key])
    path = preferences_path()
    try:
        preferences = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        preferences = {}
    if not isinstance(preferences, dict):
        preferences = {}
    for key, value in preferences.items():
        if not isinstance(key, str) or not key.startswith(
            ("JARVIS_", "OPENAI_", "BRAVE_", "UI_")
        ):
            continue
        # These identify the active instance or the configuration itself, and
        # must never be resurrected from persisted settings.
        if key in {
            "JARVIS_INSTANCE_TOKEN",
            "JARVIS_INSTANCE_ID",
            "JARVIS_CONFIG_FILE",
            "JARVIS_DATA_DIR",
        }:
            continue
        if isinstance(value, (str, int, float, bool)):
            os.environ.setdefault(
                key, str(value).lower() if isinstance(value, bool) else str(value)
            )
    for key, value in defaults.items():
        if value is not None:
            os.environ.setdefault(key, value)


def model_dir(kind: str) -> Path:
    if kind not in {"sensevoice", "kokoro"}:
        raise ValueError("Unknown speech model")
    variable = (
        "JARVIS_STT_MODEL_DIR" if kind == "sensevoice" else "JARVIS_TTS_MODEL_DIR"
    )
    explicit = os.getenv(variable, "").strip()
    if explicit:
        base = user_data_dir() if getattr(sys, "frozen", False) else resource_root()
        return _absolute(explicit, base)
    # Reuse development downloads, but never put user data in an app bundle.
    development = resource_root() / "models" / kind
    if not getattr(sys, "frozen", False) and development.is_dir():
        return development
    return user_data_dir() / "models" / kind
