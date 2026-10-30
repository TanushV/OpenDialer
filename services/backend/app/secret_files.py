"""Authenticated credential files; the encryption key never lives beside the data.

Desktop: native OS credential store only. Servers: an explicitly supplied deployment
secret. No plaintext fallback. Plain 0.4/0.5 files are replaced atomically on upgrade.
"""

import json
import os
import sys
import tempfile
import threading
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

PREFIX = b"OPENDIALER-SECRET-1\n"
KEY_SERVICE = "OpenDialer credential encryption"
KEY_ACCOUNT = "storage-v1"
_KEY_LOCK = threading.Lock()


class SecretStorageError(RuntimeError):
    pass


@contextmanager
def file_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, "a+b") as stream:
        if os.name == "nt":
            import msvcrt

            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        yield


def native_backend():
    if sys.platform == "darwin":
        from keyring.backends.macOS import Keyring

        return Keyring()
    if sys.platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring

        return WinVaultKeyring()
    raise SecretStorageError(
        "Set OPENDIALER_SECRET_KEY_FILE (or OPENDIALER_SECRET_KEY) to a persistent Fernet key for this server."
    )


@lru_cache(maxsize=1)
def native_key():
    backend = native_backend()
    directory = (
        Path(os.environ["LOCALAPPDATA"]) / "OpenDialer" / "Security"
        if sys.platform == "win32"
        else Path.home() / "Library/Application Support/OpenDialer/Security"
    )
    with _KEY_LOCK, file_lock(directory / "key.lock"):
        value = backend.get_password(KEY_SERVICE, KEY_ACCOUNT)
        if value is None:
            value = Fernet.generate_key().decode()
            backend.set_password(KEY_SERVICE, KEY_ACCOUNT, value)
        return value


def cipher():
    try:
        value = os.environ.get("OPENDIALER_SECRET_KEY")
        filename = os.environ.get("OPENDIALER_SECRET_KEY_FILE")
        if value and filename:
            raise ValueError("Choose one key source")
        if filename:
            value = Path(filename).read_text().strip()
        return Fernet((value or native_key()).encode())
    except SecretStorageError:
        raise
    except Exception:
        raise SecretStorageError(
            "Credential storage is unavailable. Unlock the OS credential store or restore the configured server key. No plaintext fallback is used."
        ) from None


def write_private(path, value, *, encrypted=True):
    path = Path(path)
    content = json.dumps(value).encode()
    if encrypted:
        content = PREFIX + cipher().encrypt(content)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".secret-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_private(path):
    path = Path(path)
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return {}
    try:
        if content.startswith(PREFIX):
            return json.loads(cipher().decrypt(content[len(PREFIX) :]))
        value = json.loads(content)
        if not isinstance(value, dict):
            raise ValueError("Invalid credential document")
        write_private(path, value)
        return value
    except (InvalidToken, ValueError, UnicodeError):
        raise SecretStorageError(
            "Saved credentials cannot be decrypted or are damaged. Restore the original credential-store key; the saved file has not been overwritten."
        ) from None


def protect_existing(directory):
    directory = Path(directory)
    for pattern in ("config/*.json", "codex/*/auth.json", "managed-session.json", "managed-connection.json"):
        for path in directory.glob(pattern):
            read_private(path)
