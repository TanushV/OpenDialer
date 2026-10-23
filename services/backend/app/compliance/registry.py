"""Imported do-not-call registries: one immutable bitmap chunk per area code, published atomically."""

import base64
import hashlib
import json
import zlib
from collections import OrderedDict
from threading import Lock

from sqlalchemy import select

from app.db import MODELS, audit, doc, put, uid, update

_CHUNKS, _CHUNKS_LOCK, _CHUNK_LIMIT = OrderedDict(), Lock(), 32


class RegistryError(ValueError):
    pass


def published_snapshots(s, p):
    scope = ("registry_snapshots", p["tenant_id"], p["seller_id"])
    records = s.info.get("row_cache", {}).get(scope)
    if records is None:
        cache = s.info.setdefault("published_registries", {})
        if scope not in cache:
            model = MODELS["registry_snapshots"]
            cache[scope] = list(
                s.scalars(
                    select(model).where(
                        model.tenant_id == p["tenant_id"],
                        model.seller_id == p["seller_id"],
                        model.ctx_key.in_(("national", "state")),
                        model.ctx_status == "published",
                    )
                )
            )
        records = cache[scope]
    return [r for r in records if r.data.get("published") and r not in s.deleted]


def valid_endpoint(phone):
    return isinstance(phone, str) and len(phone) == 12 and phone.startswith("+1") and phone[1:].isdigit()


def _chunk(s, snapshot, part, npa):
    key = (part["id"], part["checksum"])
    with _CHUNKS_LOCK:
        if key in _CHUNKS:
            _CHUNKS.move_to_end(key)
            return _CHUNKS[key]
    child = s.get(MODELS["registry_snapshots"], part["id"])
    if not child or (
        child.tenant_id,
        child.seller_id,
        child.data.get("publication_id"),
        child.data.get("npa"),
    ) != (
        snapshot.tenant_id,
        snapshot.seller_id,
        snapshot.id,
        npa,
    ):
        raise RegistryError("Invalid registry chunk identity")
    bitmap = RegistryBitmap.load(base64.b64decode(child.data["bitmap"]), part["checksum"])
    with _CHUNKS_LOCK:
        _CHUNKS[key] = bitmap
        while len(_CHUNKS) > _CHUNK_LIMIT:
            _CHUNKS.popitem(last=False)
    return bitmap


def registry_contains(s, snapshot, phone):
    if not valid_endpoint(phone):
        raise RegistryError("Malformed endpoint cannot be screened; it is not cleared")
    part = snapshot.data.get("chunks", {}).get(phone[2:5])
    return bool(part) and _chunk(s, snapshot, part, phone[2:5]).contains(phone)


def publication_receipt(stage):
    return {
        "id": stage.id,
        "version": stage.data["published_version"],
        "current_version": stage.version,
        "published": stage.data["published"],
        "checksum": stage.data["checksum"],
        "count": stage.data["count"],
    }


def _validated(service, data):
    from uuid import UUID

    from app.service import dt

    kind = data.get("kind")
    npas = sorted(set(data.get("npas", [])))
    request_id = str(UUID(data["request_id"])) if data.get("request_id") else uid()
    refreshed = dt(data["refreshed_at"])
    expiries = {key: dt(data[key]) for key in ("expires_at", "authorization_expires_at") if data.get(key)}
    if any(not at or not at.tzinfo for at in expiries.values()):
        raise ValueError("Source expiry requires an offset")
    if (
        kind not in {"national", "state"}
        or not data.get("subscription_reference")
        or not npas
        or any(
            not isinstance(n, str) or len(n) != 3 or not n.isdigit() or n[0] not in "23456789" for n in npas
        )
        or not refreshed
        or not refreshed.tzinfo
        or refreshed > service.clock()
    ):
        raise ValueError("Source authorization/coverage/timestamp required")
    grouped = {name: {} for name in ("numbers", "add", "remove")}
    for name in grouped:
        for number in data.get(name, []):
            if not valid_endpoint(number) or number[2:5] not in npas:
                raise ValueError("Invalid or uncovered endpoint")
            grouped[name].setdefault(number[2:5], set()).add(number)
    if any(grouped["add"].get(n, set()) & grouped["remove"].get(n, set()) for n in npas):
        raise ValueError("A change cannot both add and remove the same endpoint")
    if data.get("base_snapshot_id") and (
        not data.get("request_id") or type(data.get("expected_version")) is not int or "numbers" in data
    ):
        raise ValueError("A change list needs an identified original version")
    if not data.get("base_snapshot_id") and (data.get("add") or data.get("remove")):
        raise ValueError("Change list requires the original snapshot")
    return kind, npas, request_id, refreshed, expiries, grouped


def publish_registry(service, p, data):
    """Stage one area code at a time; replace the published pointer only at the end.

    A lost response resumes with the same request and file. Failed staging never retires the
    currently published source.
    """
    from app.compliance.screening import active_admin
    from app.service import fail

    try:
        kind, npas, request_id, refreshed, expiries, grouped = _validated(service, data)
    except (ValueError, TypeError, KeyError):
        fail(422, "Valid authorized source, area codes, normalized endpoints and timestamp required")
    jurisdiction = data.get("jurisdiction", "")
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    key = "registry-import:" + request_id

    def authorized(s):
        service.db.authorize_stream(s, p)
        if not active_admin(service, s, p) or not service._coach_authority(s, p):
            fail(403, "Current administrator authority is required")

    def current(s):
        return [
            r
            for r in published_snapshots(s, p)
            if r.data["kind"] == kind and r.data.get("jurisdiction", "") == jurisdiction
        ]

    with service.db.transaction() as s:
        authorized(s)
        stage = service._by_key(s, p, "registry_snapshots", key)
        if stage and stage.data.get("input_digest") != digest:
            fail(409, "Registry request identity already belongs to a different file")
        if stage and stage.data.get("status") == "published":
            return publication_receipt(stage)
        previous = current(s)
        base = (
            service.required(s, "registry_snapshots", data["base_snapshot_id"], p)
            if data.get("base_snapshot_id")
            else None
        )
        if base and (
            base.id not in {r.id for r in previous}
            or base.version != data["expected_version"]
            or set(base.data.get("npas", [])) != set(npas)
        ):
            fail(409, "Change source changed or coverage differs; reload the current publication")
        if not stage:
            stage = put(
                s,
                "registry_snapshots",
                p["tenant_id"],
                p["seller_id"],
                {
                    "kind": kind,
                    "jurisdiction": jurisdiction,
                    "npas": npas,
                    "subscription_reference": data["subscription_reference"],
                    **{name: value.isoformat() for name, value in expiries.items()},
                    "refreshed_at": refreshed.isoformat(),
                    "published": False,
                    "status": "staging",
                    "input_digest": digest,
                    "expected_publications": [[r.id, r.version] for r in previous],
                    "chunks": {},
                    "synthetic": service.settings.simulation,
                },
                key=key,
                now=service.clock(),
            )
        stage_id, base_data = stage.id, doc(base) if base else None

    count = 0
    for npa in npas:
        bits = None
        part = (base_data or {}).get("chunks", {}).get(npa)
        if part:
            with service.db.transaction(False) as s:
                bits = _chunk(
                    s, service.required(s, "registry_snapshots", base_data["id"], p), part, npa
                ).regions[npa]
        added = grouped["add" if base_data else "numbers"].get(npa, set())
        if bits is None and not added:
            continue
        bits = bytearray(bits) if bits is not None else bytearray(1_250_000)
        for number in added:
            suffix = int(number[5:])
            bits[suffix >> 3] |= 1 << (suffix & 7)
        for number in grouped["remove"].get(npa, set()):
            suffix = int(number[5:])
            bits[suffix >> 3] &= ~(1 << (suffix & 7))
        payload = RegistryBitmap({npa: bits}).dump()
        part_hash, part_count = hashlib.sha256(payload).hexdigest(), sum(value.bit_count() for value in bits)
        count += part_count
        with service.db.transaction() as s:
            authorized(s)
            stage = service.required(s, "registry_snapshots", stage_id, p)
            if stage.data.get("status") == "published":
                return publication_receipt(stage)
            child = service._by_key(s, p, "registry_snapshots", f"{stage_id}:{npa}") or put(
                s,
                "registry_snapshots",
                p["tenant_id"],
                p["seller_id"],
                {
                    "kind": kind + "_chunk",
                    "publication_id": stage_id,
                    "npa": npa,
                    "bitmap": base64.b64encode(payload).decode(),
                    "checksum": part_hash,
                },
                key=f"{stage_id}:{npa}",
                now=service.clock(),
            )
            if child.data["checksum"] != part_hash:
                fail(409, "Staged registry chunk changed; publication withheld")
            chunk = {"id": child.id, "checksum": part_hash, "count": part_count}
            update(stage, chunks={**stage.data["chunks"], npa: chunk}, cursor=npa)
    if data.get("count") is not None and data["count"] != count:
        fail(422, "Registry count mismatch; prior publication retained")
    with service.db.transaction() as s:
        authorized(s)
        stage = service.required(s, "registry_snapshots", stage_id, p)
        if stage.data.get("status") == "published":
            return publication_receipt(stage)
        published = current(s)
        if sorted([[r.id, r.version] for r in published]) != sorted(stage.data["expected_publications"]):
            fail(409, "Published source changed while staging; review the new source")
        for row in published:
            update(row, published=False)
        checksum = hashlib.sha256(
            json.dumps(sorted((n, c["checksum"]) for n, c in stage.data["chunks"].items())).encode()
        ).hexdigest()
        update(
            stage,
            published=True,
            status="published",
            published_version=stage.version + 1,
            checksum=checksum,
            count=count,
        )
        audit(
            s,
            p["tenant_id"],
            p["seller_id"],
            p["user_id"],
            "registry.published",
            stage.id,
            service.clock(),
            {"checksum": checksum, "count": count},
        )
        return publication_receipt(stage)


class RegistryBitmap:
    """Exact per-area-code suffix bitsets. Restricted to compliance, never scheduler features."""

    def __init__(self, regions):
        self.regions = regions

    @classmethod
    def build(cls, numbers):
        regions = {}
        for number in numbers:
            if not valid_endpoint(number):
                raise RegistryError("Registry input must contain normalized US numbers")
            npa, suffix = number[2:5], int(number[5:])
            bits = regions.setdefault(npa, bytearray(1_250_000))
            bits[suffix >> 3] |= 1 << (suffix & 7)
        return cls(regions)

    def contains(self, number):
        if not valid_endpoint(number):
            raise RegistryError("Malformed endpoint cannot be screened; it is not cleared")
        bits = self.regions.get(number[2:5])
        value = int(number[5:])
        return bool(bits and bits[value >> 3] & (1 << (value & 7)))

    def dump(self):
        payload = {npa: bytes(bits).hex() for npa, bits in sorted(self.regions.items())}
        return zlib.compress(json.dumps(payload, sort_keys=True).encode(), 9)

    @classmethod
    def load(cls, payload, checksum):
        if hashlib.sha256(payload).hexdigest() != checksum:
            raise RegistryError("Registry checksum mismatch")
        try:
            raw = json.loads(zlib.decompress(payload))
            result = {k: bytearray.fromhex(v) for k, v in raw.items()}
        except (ValueError, TypeError, AttributeError, zlib.error) as exc:
            raise RegistryError("Invalid registry bitmap") from exc
        if any(len(v) != 1_250_000 or not k.isdigit() or len(k) != 3 for k, v in result.items()):
            raise RegistryError("Invalid bitmap snapshot")
        return cls(result)
