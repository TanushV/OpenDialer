import base64
import hashlib
import json
import os
import re
import shutil
import threading
from contextlib import ExitStack, contextmanager
from datetime import timedelta
from pathlib import Path

import jwt
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pwdlib import PasswordHash

from app.db import get, put, uid

PASSWORDS = PasswordHash.recommended()
DUMMY_HASH = PASSWORDS.hash("unusable-dummy-password")
JOURNAL_LIMIT = 8 * 1024 * 1024
ZERO_HASH = "0" * 64


class Vault:
    def __init__(self, directory, key):
        self.directory = Path(directory) / "secrets"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))
        self._journal_lock = threading.RLock()
        self._verified_journals = set()

    def read(self, tenant_id):
        path = self.directory / (tenant_id + ".enc")
        return json.loads(self.cipher.decrypt(path.read_bytes())) if path.exists() else {}

    def write(self, tenant_id, values, *, expected=None, advance_version=False):
        with self._locked_journal("provider-config"):
            current = self.read(tenant_id)
            if expected is not None and any(current.get(k) != v for k, v in expected.items()):
                raise HTTPException(409, "Provider configuration changed before publication")
            data = {**current, **{k: v for k, v in values.items() if v is not None}}
            if advance_version:
                data["version"] = current.get("version", 0) + 1
            path = self.directory / (tenant_id + ".enc")
            temp = path.with_suffix("." + uid() + ".tmp")
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(self.cipher.encrypt(json.dumps(data).encode()))
                    f.flush()
                    os.fsync(f.fileno())
                temp.replace(path)
                self._sync_directory(self.directory)
            finally:
                temp.unlink(missing_ok=True)

    def _journal_path(self, name, suffix=".enc"):
        if not re.fullmatch(r"[a-z][a-z_-]{0,31}", name):
            raise ValueError("Invalid journal name")
        path = self.directory.parent / (name + "-journal" + suffix)
        if path.is_symlink():
            raise HTTPException(409, "Journal path cannot be a symbolic link")
        return path

    @contextmanager
    def _locked_journal(self, name):
        # The same protected directory can be used by authority and background worker.
        with self._journal_lock:
            fd = os.open(self._journal_path(name, ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                if os.name == "nt":
                    import msvcrt

                    if os.fstat(fd).st_size == 0:
                        os.write(fd, b"\0")
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)

    @staticmethod
    def _sync_directory(path):
        if os.name != "nt":
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _save_journal_head(self, name, state):
        path = self._journal_path(name, ".head")
        temp = path.with_name(path.name + "." + uid() + ".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            encoded = self.cipher.encrypt(json.dumps(state, sort_keys=True).encode())
            if os.write(fd, encoded) != len(encoded):
                raise OSError("Incomplete journal checkpoint write")
            os.fsync(fd)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        finally:
            os.close(fd)
        temp.replace(path)
        self._sync_directory(path.parent)

    def _journal_head(self, name):
        path = self._journal_path(name, ".head")
        if not path.exists():
            return None
        return json.loads(self.cipher.decrypt(path.read_bytes()))

    @staticmethod
    def _journal_hash(sequence, previous, record):
        return hashlib.sha256(
            json.dumps(
                [sequence, previous, record], sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()

    def _scan_journal(self, name, *, collect, required=None):
        head = self._journal_head(name)
        state = {"sequence": 0, "hash": ZERO_HASH, "segment": 0, "bytes": 0}
        result, head_seen, chained = [], head is None or head.get("sequence") == 0, False
        required_seen = required is None or required == {"sequence": 0, "hash": ZERO_HASH, "segment": 0}
        segments = sorted(self.directory.parent.glob(name + "-journal-*.enc"))
        for index, path in enumerate(segments + [self._journal_path(name)], 1):
            if path.is_symlink() or index <= len(segments) and path.name != f"{name}-journal-{index:08d}.enc":
                raise ValueError("Missing or redirected journal segment")
            if not path.exists():
                if index <= len(segments):
                    raise ValueError("Missing journal segment")
                continue
            state["bytes"] = path.stat().st_size
            with path.open("rb") as stream:
                while line := stream.readline(1_000_001):
                    if len(line) > 1_000_000 or not line.endswith(b"\n"):
                        raise ValueError("Incomplete journal record")
                    value = json.loads(self.cipher.decrypt(line.strip()))
                    if not isinstance(value, dict):
                        raise ValueError("Invalid journal record")
                    sequence = state["sequence"] + 1
                    if value.get("journal_version") == 1:
                        chained = True
                        if value.get("sequence") != sequence or value.get("previous") != state["hash"]:
                            raise ValueError("Journal sequence or chain conflict")
                        value = value["record"]
                        if not isinstance(value, dict):
                            raise ValueError("Invalid journal record")
                    elif chained:
                        raise ValueError("Unframed record after journal cutover")
                    # Legacy encrypted denial lines are included in the first chained checkpoint.
                    state["hash"] = self._journal_hash(sequence, state["hash"], value)
                    state["sequence"] = sequence
                    if head and sequence == head["sequence"]:
                        if state["hash"] != head["hash"]:
                            raise ValueError("Journal checkpoint conflict")
                        head_seen = True
                    if required and sequence == required.get("sequence"):
                        if state["hash"] != required.get("hash"):
                            raise ValueError("Backup journal checkpoint conflict")
                        required_seen = True
                    if collect:
                        result.append(value)
            if index <= len(segments):
                state["segment"], state["bytes"] = index, 0
        if not head_seen or not required_seen:
            raise ValueError("Acknowledged journal records are missing")
        return result, state

    def append_journal(self, name, record):
        if not isinstance(record, dict):
            raise ValueError("Journal record must be an object")
        with self._locked_journal(name):
            try:
                path = self._journal_path(name)
                state = self._journal_head(name)
                size = path.stat().st_size if path.exists() else 0
                if name not in self._verified_journals or not state or size != state["bytes"]:
                    _, state = self._scan_journal(name, collect=False)
                    self._verified_journals.add(name)
            except Exception:
                raise HTTPException(
                    409, "Journal cannot be verified; recover its protected copy before resuming"
                ) from None
            sequence = state["sequence"] + 1
            frame = {"journal_version": 1, "sequence": sequence, "previous": state["hash"], "record": record}
            encoded = self.cipher.encrypt(json.dumps(frame, sort_keys=True, allow_nan=False).encode()) + b"\n"
            if len(encoded) > 1_000_000:
                raise ValueError("Journal record exceeds its bound")
            if state["bytes"] and state["bytes"] + len(encoded) > JOURNAL_LIMIT:
                state["segment"] += 1
                target = self._journal_path(name, f"-{state['segment']:08d}.enc")
                if target.exists():
                    raise HTTPException(409, "Journal rotation conflicts with an existing segment")
                path.rename(target)
                self._sync_directory(path.parent)
                state["bytes"] = 0
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                if os.write(descriptor, encoded) != len(encoded):
                    raise OSError("Incomplete journal write")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._sync_directory(path.parent)
            self._save_journal_head(
                name,
                {
                    **state,
                    "sequence": sequence,
                    "hash": self._journal_hash(sequence, state["hash"], record),
                    "bytes": state["bytes"] + len(encoded),
                },
            )

    def read_journal(self, name):
        with self._locked_journal(name):
            try:
                result, _ = self._scan_journal(name, collect=True)
                self._verified_journals.add(name)
                return result
            except Exception:
                raise HTTPException(
                    409, "Journal cannot be verified; recover its protected copy before resuming"
                ) from None

    def journal_checkpoint(self, name):
        with self._locked_journal(name):
            try:
                _, state = self._scan_journal(name, collect=False)
                self._save_journal_head(name, state)
                return {k: state[k] for k in ("sequence", "hash", "segment")}
            except Exception:
                raise HTTPException(409, "Journal checkpoint cannot be verified") from None

    def verify_journal_checkpoint(self, name, checkpoint):
        with self._locked_journal(name):
            try:
                self._scan_journal(name, collect=False, required=checkpoint)
            except Exception:
                raise HTTPException(409, "Backup journal checkpoint cannot be verified") from None

    def sync_journal(self, name):
        """Confirm repaired storage before releasing an earlier failed append hold."""
        self.journal_checkpoint(name)
        path = self._journal_path(name)
        descriptor = os.open(path, os.O_RDWR)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._sync_directory(path.parent)

    def copy_recovery_bundle(self, backup, destination):
        """Operator-owned encrypted disk copy; retain originals and publish last."""
        destination = Path(destination).absolute()
        if not destination.is_dir() or destination.resolve() != destination:
            raise ValueError("Backup copy destination must be an existing non-symlink directory")
        if os.name != "nt" and destination.stat().st_mode & 0o022:
            raise ValueError("Backup copy destination cannot be writable by other users")
        final = destination / backup.stem
        temporary = destination / (".copy-" + uid())
        temporary.mkdir(mode=0o700)
        try:
            with ExitStack() as locks:
                names = ("authorization", "consent", "effects", "suppression")
                for name in names:
                    locks.enter_context(self._locked_journal(name))
                checkpoints, paths = {}, [backup]
                for name in names:
                    # Legacy authorization is sanitized before backup, not a chained journal.
                    if name != "authorization":
                        _, state = self._scan_journal(name, collect=False)
                        checkpoints[name] = {k: state[k] for k in ("sequence", "hash", "segment")}
                    paths.extend(sorted(self.directory.parent.glob(name + "-journal*.enc")))
                    head = self._journal_path(name, ".head")
                    if head.exists():
                        paths.append(head)
                files = {}
                for source in paths:
                    if source.is_symlink() or not source.is_file():
                        raise ValueError("Recovery source must be a protected regular file")
                    digest = hashlib.sha256()
                    target = temporary / source.name
                    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with source.open("rb") as src, os.fdopen(descriptor, "wb") as dst:
                        while chunk := src.read(1024 * 1024):
                            digest.update(chunk)
                            dst.write(chunk)
                        dst.flush()
                        os.fsync(dst.fileno())
                    with target.open("rb") as copied:
                        if hashlib.file_digest(copied, "sha256").hexdigest() != digest.hexdigest():
                            raise OSError("Recovery copy checksum mismatch")
                    files[source.name] = digest.hexdigest()
                manifest = self.cipher.encrypt(
                    json.dumps(
                        {
                            "schema": 1,
                            "backup_id": backup.stem,
                            "files": files,
                            "journal_checkpoints": checkpoints,
                        },
                        sort_keys=True,
                    ).encode()
                )
                descriptor = os.open(temporary / "manifest.enc", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(manifest)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._sync_directory(temporary)
                if final.exists():
                    raise FileExistsError("An archive with this backup identity already exists")
                temporary.rename(final)
                self._sync_directory(destination)
            return {
                "verified": True,
                "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
                "files": len(files),
                "separate_device": destination.stat().st_dev != backup.stat().st_dev,
                "off_host_verified": False,
            }
        finally:
            # Only our unpublished staging directory; accepted archives and source stay intact.
            if temporary.exists():
                shutil.rmtree(temporary)

    def sanitize_legacy_authorization(self):
        """Retain the installed format's recovery IDs, never its old full call body."""
        from app.accounting import EFFECT_FIELDS

        with self._locked_journal("authorization"):
            path = self._journal_path("authorization")
            if not path.exists():
                return
            temp = path.with_name(path.name + ".sanitizing")
            if temp.is_symlink():
                raise HTTPException(409, "Legacy journal output path is not protected")
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "wb") as output, path.open("rb") as source:
                    while line := source.readline(1_000_001):
                        if len(line) > 1_000_000 or not line.endswith(b"\n"):
                            raise ValueError("Incomplete legacy authorization record")
                        entry = json.loads(self.cipher.decrypt(line.strip()))
                        if not all(entry.get(k) for k in ("tenant_id", "seller_id", "call_id")):
                            raise ValueError("Legacy authorization record lacks scope")
                        clean = {
                            k: v
                            for k, v in entry.items()
                            if k
                            in {
                                "call_id",
                                "tenant_id",
                                "seller_id",
                                "user_id",
                                "at",
                                "provider_id",
                                "command_id",
                                "command_hash",
                            }
                        }
                        clean["intent"] = {
                            k: v
                            for k, v in entry.get("intent", {}).items()
                            if k in EFFECT_FIELDS["call_attempts"]
                        }
                        output.write(self.cipher.encrypt(json.dumps(clean).encode()) + b"\n")
                    output.flush()
                    os.fsync(output.fileno())
                temp.replace(path)
                self._sync_directory(path.parent)
            finally:
                temp.unlink(missing_ok=True)


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
