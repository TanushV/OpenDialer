import hashlib
import json
import secrets
import threading
from datetime import timedelta
from pathlib import Path

from fastapi import HTTPException

from app.platform.db import MODELS
from app.platform.secret_files import file_lock, protect_existing, read_private, write_private


class ConfigStore:
    """Per-tenant (and per-user plan) encrypted provider configuration.

    Every authority process sharing the data directory reads the same files; writes replace a
    file atomically. Edits carrying a request identity are idempotent and version-checked.
    """

    def __init__(self, directory):
        self.directory = Path(directory) / "config"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.Lock()
        with file_lock(self.directory / ".lock"):
            protect_existing(directory)

    def read(self, key):
        return read_private(self.directory / (key + ".json"))

    def write(self, key, values, *, expected=None, advance_version=False, edit=None):
        with self._lock, file_lock(self.directory / ".lock"):
            current = self.read(key)
            receipts = dict(current.get("_config_receipts", {}))
            if edit is not None:
                request_id, expected_version = edit
                digest = hashlib.sha256(
                    json.dumps([expected_version, values], sort_keys=True).encode()
                ).hexdigest()
                if request_id in receipts:
                    if receipts[request_id] != digest:
                        raise HTTPException(409, "Provider request identity has different input")
                    return
                receipts[request_id] = digest
                while len(receipts) > 128:
                    receipts.pop(next(iter(receipts)))
            if expected is not None and any(current.get(k) != v for k, v in expected.items()):
                raise HTTPException(409, "Provider configuration changed before publication")
            data = {**current, **{k: v for k, v in values.items() if v is not None}}
            if advance_version:
                data["version"] = current.get("version", 0) + 1
            if edit is not None:
                data["_config_receipts"] = receipts
            path = self.directory / (key + ".json")
            write_private(path, data)


def _token_hash(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


class Sessions:
    """R3.4: one user, no passwords or roles. The desktop trades its launch token for a session, held only in this
    process's memory; after a backend restart it asks again with the same launch token."""

    def __init__(self, seconds):
        self.seconds, self.tokens, self.lock = seconds, {}, threading.Lock()

    def issue(self, user, now):
        secret = secrets.token_urlsafe(32)
        with self.lock:
            self.tokens = {k: v for k, v in self.tokens.items() if v[1] > now}
            self.tokens[_token_hash(secret)] = (user.id, now + timedelta(seconds=self.seconds))
        return {
            "access_token": secret,
            "token_type": "bearer",
            "user_id": user.id,
            "tenant_id": user.tenant_id,
            "seller_id": user.seller_id,
            "name": name_of(user),
        }

    def check(self, db, token, now):
        with self.lock:
            found = self.tokens.get(_token_hash(token or ""))
        if not found or found[1] <= now:
            raise HTTPException(401, "Session expired or invalid")
        with db.transaction(False) as s:
            user = s.get(MODELS["users"], found[0])
            if user is None:
                raise HTTPException(401, "Session expired or invalid")
            return principal_of(user)


def principal_of(user):
    """Who is acting: the one user, in the workspace's one tenant and seller."""
    return {
        "user_id": user.id,
        "tenant_id": user.tenant_id,
        "seller_id": user.seller_id,
        "name": name_of(user),
    }


def name_of(user):
    return user.data["name"] if "name" in user.data else "Operator"
