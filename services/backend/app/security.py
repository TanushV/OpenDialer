import hashlib
import json
import secrets
import threading
from datetime import timedelta
from pathlib import Path

from fastapi import HTTPException

from app.db import get, put, uid


class LazyPasswordHash:
    """Argon2 loads on first use: its native libraries are not part of authority startup."""

    def __init__(self):
        self._hasher, self._lock = None, threading.Lock()

    def _get(self):
        with self._lock:
            if self._hasher is None:
                from pwdlib import PasswordHash

                self._hasher = PasswordHash.recommended()
            return self._hasher

    def hash(self, password):
        return self._get().hash(password)

    def verify(self, password, hashed):
        return self._get().verify(password, hashed)


PASSWORDS = LazyPasswordHash()


class ConfigStore:
    """Per-tenant (and per-user plan) provider configuration as plain JSON files.

    Every authority process sharing the data directory reads the same files; writes replace a
    file atomically. Edits carrying a request identity are idempotent and version-checked.
    """

    def __init__(self, directory):
        self.directory = Path(directory) / "config"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.Lock()

    def read(self, key):
        try:
            return json.loads((self.directory / (key + ".json")).read_text())
        except FileNotFoundError:
            return {}

    def write(self, key, values, *, expected=None, advance_version=False, edit=None):
        with self._lock:
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
                if current.get("version", 0) != expected_version:
                    raise HTTPException(409, "Provider configuration changed; reload before saving")
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
            temporary = path.with_suffix("." + uid() + ".tmp")
            temporary.write_text(json.dumps(data))
            temporary.replace(path)


def _token_hash(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def issue_session(s, settings, user, now):
    """An opaque bearer token: the session row's ID plus a random secret stored only as a hash."""
    session_id, secret = uid(), secrets.token_urlsafe(32)
    expires = now + timedelta(seconds=settings.session_seconds)
    put(
        s,
        "sessions",
        user.tenant_id,
        user.seller_id,
        {
            "user_id": user.id,
            "expires_at": expires.isoformat(),
            "revoked": False,
            "token_hash": _token_hash(secret),
        },
        object_id=session_id,
        now=now,
    )
    return {
        "access_token": f"{session_id}.{secret}",
        "token_type": "bearer",
        "user_id": user.id,
        "tenant_id": user.tenant_id,
        "seller_id": user.seller_id,
        "role": user.data["role"],
        "name": user.data.get("name", "Operator"),
    }


def decode_session(db, settings, token, now, *, logout_receipt_only=False):
    from datetime import datetime

    session_id, _, secret = (token or "").partition(".")
    with db.transaction(False) as s:
        session = get(s, "sessions", session_id) if session_id and secret else None
        user = get(s, "users", session.data.get("user_id"), session.tenant_id) if session else None
        valid = bool(
            session
            and user
            and secrets.compare_digest(session.data.get("token_hash", ""), _token_hash(secret))
            and session.seller_id == user.seller_id
        )
        completed_logout = bool(valid and logout_receipt_only and session.data.get("logout_completed_at"))
        if not valid or (
            not completed_logout
            and (
                session.data["revoked"]
                or datetime.fromisoformat(session.data["expires_at"]) <= now
                or user.data.get("disabled")
            )
        ):
            raise HTTPException(401, "Session expired or invalid")
        return {
            "user_id": user.id,
            "tenant_id": user.tenant_id,
            "seller_id": user.seller_id,
            "role": user.data["role"],
            "name": user.data.get("name"),
            "session_id": session.id,
            "platform_admin": user.data.get("platform_admin", False),
        }


def admin(principal):
    if principal["role"] not in {"owner", "admin"}:
        raise HTTPException(403, "Administrator role required")
