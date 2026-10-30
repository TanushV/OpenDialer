import hashlib
import json
import secrets
import threading
from collections import deque
from datetime import timedelta
from pathlib import Path
from time import monotonic

from fastapi import HTTPException
from sqlalchemy import select

from app.db import MODELS, put, uid
from app.secret_files import file_lock, protect_existing, read_private, write_private


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


class LoginLimiter:
    """Bound password-hash work per source and per authority, without trusting forwarded headers."""

    def __init__(self):
        self.sources, self.total, self.lock = {}, deque(), threading.Lock()

    def check(self, source):
        now = monotonic()
        with self.lock:
            self.sources = {k: v for k, v in self.sources.items() if v and v[-1] > now - 60}
            entries = self.sources.setdefault(source, deque())
            for queue in (entries, self.total):
                while queue and queue[0] <= now - 60:
                    queue.popleft()
            if len(entries) >= 10 or len(self.total) >= 100:
                raise HTTPException(
                    429, "Too many sign-in attempts. Try again in a minute.", headers={"Retry-After": "60"}
                )
            entries.append(now)
            self.total.append(now)


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
            write_private(path, data)


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
    sessions, users = MODELS["sessions"], MODELS["users"]
    with db.transaction(False) as s:
        found = (
            s.execute(
                select(sessions, users)
                .join(users, users.id == sessions.data["user_id"].as_string())
                .where(sessions.id == session_id, users.tenant_id == sessions.tenant_id)
            ).first()
            if session_id and secret
            else None
        )
        session, user = found if found else (None, None)
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
