"""Local-only CurseForge credential access for the server and detached workers."""

from __future__ import annotations

import getpass
import hashlib
import os
from pathlib import Path
import sys
import warnings

from .models import SourceError

ENV = "CURSEFORGE_API_KEY"
SERVICE = "Modpack Builder (CurseForge)"
MARKER = ".curseforge-keyring"
MAX_KEY_LENGTH = 4096


def validate_key(value: str) -> str:
    key = value.strip()
    if (not key or key.startswith(("https://", "http://")) or len(key) > MAX_KEY_LENGTH
            or any(not 33 <= ord(char) <= 126 for char in key)):
        raise SourceError("API_KEY_INVALID_FORMAT: paste the key itself from the CurseForge developer console")
    return key


def account(root: Path) -> str:
    # Keep isolated installs and test state directories from sharing credentials.
    identity = os.path.normcase(str(root.resolve()))
    return hashlib.sha256(os.fsencode(identity)).hexdigest()


def system_keyring():
    # Select only built-in OS services. Never fall through to a plaintext backend.
    if sys.platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring
        return WinVaultKeyring()
    if sys.platform == "darwin":
        from keyring.backends.macOS import Keyring
        return Keyring()
    if sys.platform.startswith("linux"):
        from keyring.backends.SecretService import Keyring
        return Keyring()
    raise RuntimeError("No configured system credential store for this platform")


def _keyring(operation, root, *args):
    try:
        return getattr(system_keyring(), operation)(SERVICE, account(root), *args)
    except Exception:
        # Do not surface third-party exception text, which could contain credentials.
        raise SourceError("API_KEYRING_UNAVAILABLE: unlock the system credential store or use CURSEFORGE_API_KEY") from None


def key_status(root: Path) -> dict:
    """Show the configured source without unlocking the credential store."""
    if os.environ.get(ENV):
        return {"configured": True, "source": "environment"}
    if (root / MARKER).is_file():
        return {"configured": True, "source": "system_keyring"}
    return {"configured": False, "source": None}


def effective_key(root: Path) -> str | None:
    if env := os.environ.get(ENV):
        return validate_key(env)
    if (root / MARKER).is_file():
        key = _keyring("get_password", root)
        if key is None:
            raise SourceError("API_KEYRING_ENTRY_MISSING: set the key again or run curseforge-key unset")
        return validate_key(key)
    return None


def store_key(root: Path, key: str) -> None:
    key = validate_key(key)
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    _keyring("set_password", root, key)
    # Only a marker is written to the filesystem. The secret lives in the OS vault.
    (root / MARKER).touch(mode=0o600, exist_ok=True)


def remove_key(root: Path) -> bool:
    marker = root / MARKER
    if not marker.is_file():
        return False
    if _keyring("get_password", root) is not None:
        _keyring("delete_password", root)
    marker.unlink()
    return True


def prompt_key() -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return validate_key(getpass.getpass("CurseForge API key (hidden): "))
        except (EOFError, getpass.GetPassWarning):
            raise SourceError("API_KEY_INPUT_UNAVAILABLE: run curseforge-key set in a local terminal") from None
