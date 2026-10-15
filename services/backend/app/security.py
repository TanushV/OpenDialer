import base64
import hashlib
import json
import os
from datetime import timedelta
from pathlib import Path

import jwt
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pwdlib import PasswordHash

from app.db import get, put, uid

PASSWORDS = PasswordHash.recommended()
DUMMY_HASH = PASSWORDS.hash("unusable-dummy-password")


class Vault:
    def __init__(self, directory, key):
        self.directory = Path(directory) / "secrets"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))

    def read(self, tenant_id):
        path = self.directory / (tenant_id + ".enc")
        return json.loads(self.cipher.decrypt(path.read_bytes())) if path.exists() else {}

    def write(self, tenant_id, values):
        path = self.directory / (tenant_id + ".enc")
        data = {**self.read(tenant_id), **{k: v for k, v in values.items() if v is not None}}
        temp = path.with_suffix(".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(self.cipher.encrypt(json.dumps(data).encode()))
            f.flush()
            os.fsync(f.fileno())
        temp.replace(path)


def issue_session(s, settings, user, now):
    session_id = uid()
    expires = now + timedelta(seconds=settings.session_seconds)
    put(
        s,
        "sessions",
        user.tenant_id,
        user.seller_id,
        {"user_id": user.id, "expires_at": expires.isoformat(), "revoked": False},
        object_id=session_id,
        now=now,
    )
    token = jwt.encode(
        {"sub": user.id, "jti": session_id, "tenant_id": user.tenant_id, "exp": expires},
        settings.secret_key,
        algorithm="HS256",
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "user_id": user.id,
        "tenant_id": user.tenant_id,
        "seller_id": user.seller_id,
        "role": user.data["role"],
        "name": user.data.get("name", "Operator"),
    }


def decode_session(db, settings, token, now):
    try:
        claims = jwt.decode(token, settings.secret_key, algorithms=["HS256"], options={"verify_exp": False})
        from datetime import datetime

        with db.transaction(False) as s:
            session = get(s, "sessions", claims["jti"], claims["tenant_id"])
            user = get(s, "users", claims["sub"], claims["tenant_id"])
            if (
                not session
                or session.data["revoked"]
                or datetime.fromisoformat(session.data["expires_at"]) <= now
                or not user
                or user.data.get("disabled")
            ):
                raise ValueError("Revoked or expired session")
            return {
                "user_id": user.id,
                "tenant_id": user.tenant_id,
                "seller_id": user.seller_id,
                "role": user.data["role"],
                "name": user.data.get("name"),
                "session_id": session.id,
                "platform_admin": user.data.get("platform_admin", False),
            }
    except (jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(401, "Session expired or invalid") from None


def admin(principal):
    if principal["role"] not in {"owner", "admin"}:
        raise HTTPException(403, "Administrator role required")
