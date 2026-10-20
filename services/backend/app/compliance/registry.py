import base64
import hashlib
import hmac
import json
import zlib

from sqlalchemy import select

from app.db import MODELS, audit, doc, get, put, uid, update


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
    return (
        isinstance(phone, str)
        and len(phone) == 12
        and phone.startswith("+1")
        and phone[1:].isascii()
        and phone[1:].isdigit()
    )


def registry_contains(s, snapshot, phone):
    """Chunks are immutable and only a current scoped publication can refer to them."""
    if not valid_endpoint(phone):
        raise RegistryError("Malformed endpoint cannot be screened; it is not cleared")
    if "chunks" in snapshot.data:
        part = snapshot.data["chunks"].get(phone[2:5])
        if part is None:
            return False  # An explicit covered NPA may contain zero listed endpoints.
        db = s.info["database"]
        key = (
            snapshot.tenant_id,
            snapshot.seller_id,
            snapshot.id,
            snapshot.version,
            phone[2:5],
            part["id"],
            part["checksum"],
        )
        with db.registry_lru_lock:
            if key in db.registry_lru:
                membership = db.registry_lru.pop(key)
                db.registry_lru[key] = membership
                return membership.contains(phone)
        child = get(s, "registry_snapshots", part["id"])
        if not child or (
            child.tenant_id,
            child.seller_id,
            child.data.get("publication_id"),
            child.data.get("npa"),
            child.data.get("checksum"),
        ) != (snapshot.tenant_id, snapshot.seller_id, snapshot.id, phone[2:5], part["checksum"]):
            raise RegistryError("Invalid registry chunk identity")
        membership = RegistryBitmap.load(
            base64.b64decode(child.data["bitmap"], validate=True), part["checksum"]
        )
        if set(membership.regions) != {phone[2:5]}:
            raise RegistryError("Invalid registry chunk coverage")
        with db.registry_lru_lock:
            db.registry_lru[key] = membership
            while (
                sum(sum(len(v) for v in item.regions.values()) for item in db.registry_lru.values())
                > db.registry_lru_bytes
            ):
                db.registry_lru.popitem(last=False)
        return membership.contains(phone)
    # Read compatibility for installed publications until the chunk backfill completes.
    cache = s.info.setdefault("registry_membership", {})
    key = (snapshot.id, snapshot.version, snapshot.data.get("checksum"))
    if key not in cache:
        cache[key] = (
            RegistryBitmap.load(base64.b64decode(snapshot.data["bitmap"]), snapshot.data["checksum"])
            if snapshot.data.get("bitmap")
            else frozenset(snapshot.data.get("listed_numbers", []))
        )
    membership = cache[key]
    return membership.contains(phone) if isinstance(membership, RegistryBitmap) else phone in membership


class RegistryError(ValueError):
    pass


def migrate_registry_chunks(service):
    """Resumable legacy publication backfill; identities and source times stay fixed.

    Only the final metadata swap makes chunks visible. Each immutable NPA is a
    checkpoint, so interruption leaves the old source intact and reuses prior work.
    """
    model, after = MODELS["registry_snapshots"], ""
    while True:
        with service.db.transaction(False) as s:
            page = list(
                s.scalars(
                    select(model)
                    .where(
                        model.ctx_key.in_(("national", "state")),
                        model.ctx_status == "published",
                        model.id > after,
                    )
                    .order_by(model.id)
                    .limit(500)
                )
            )
            snapshots = [(r.id, r.tenant_id, r.seller_id, r.version, dict(r.data)) for r in page]
        if not snapshots:
            return
        for identifier, tenant, seller, version, data in snapshots:
            after = identifier
            if "chunks" in data:
                continue
            regions = (
                RegistryBitmap.iter_regions(base64.b64decode(data["bitmap"], validate=True), data["checksum"])
                if data.get("bitmap")
                else (
                    (
                        npa,
                        RegistryBitmap.build(
                            [v for v in data.get("listed_numbers", []) if v[2:5] == npa]
                        ).regions.get(npa),
                    )
                    for npa in sorted(data.get("npas", []))
                )
            )
            chunks = {}
            for npa, bits in regions:
                if bits is None:
                    continue
                if npa not in data.get("npas", []):
                    raise RegistryError("Legacy membership exceeds authorized coverage")
                payload = RegistryBitmap({npa: bits}).dump()
                checksum = hashlib.sha256(payload).hexdigest()
                key = f"legacy:{identifier}:{version}:{npa}"
                with service.db.transaction() as s:
                    current = get(s, "registry_snapshots", identifier, tenant)
                    if current.version != version:
                        raise RegistryError("Registry changed during backfill; restart migration")
                    child = s.scalar(
                        select(model).where(
                            model.tenant_id == tenant, model.seller_id == seller, model.external_key == key
                        )
                    )
                    if not child:
                        child = put(
                            s,
                            "registry_snapshots",
                            tenant,
                            seller,
                            {
                                "kind": data["kind"] + "_chunk",
                                "publication_id": identifier,
                                "npa": npa,
                                "bitmap": base64.b64encode(payload).decode(),
                                "checksum": checksum,
                            },
                            key=key,
                            now=service.clock(),
                        )
                    if child.data["checksum"] != checksum:
                        raise RegistryError("Legacy checkpoint checksum changed")
                    chunks[npa] = {
                        "id": child.id,
                        "checksum": checksum,
                        "count": sum(v.bit_count() for v in bits),
                    }
            with service.db.transaction() as s:
                current = get(s, "registry_snapshots", identifier, tenant)
                if current.version != version:
                    raise RegistryError("Registry changed during backfill; restart migration")
                current.data = {k: v for k, v in data.items() if k not in {"bitmap", "listed_numbers"}}
                update(current, chunks=chunks, format_version=2)


def publication_receipt(stage):
    return {
        "id": stage.id,
        "version": stage.data["published_version"],
        "current_version": stage.version,
        "published": stage.data["published"],
        "checksum": stage.data["checksum"],
        "count": stage.data["count"],
    }


def publish_registry(service, p, data):
    """Stage one immutable NPA at a time; replace the published pointer only at the end.

    A lost response resumes with the same request and supplied file. No country bitmap
    is assembled in RAM, and failed staging cannot retire the currently usable source.
    """
    from uuid import UUID

    from app.compliance.screening import active_admin
    from app.service import dt, fail

    kind, jurisdiction = data.get("kind"), data.get("jurisdiction", "")
    try:
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
                not isinstance(n, str)
                or len(n) != 3
                or not n.isascii()
                or not n.isdigit()
                or n[0] not in "23456789"
                for n in npas
            )
            or not refreshed
            or not refreshed.tzinfo
            or refreshed > service.clock()
        ):
            raise ValueError("Source authorization/coverage/timestamp required")
        grouped = {name: {} for name in ("numbers", "add", "remove")}
        for name in grouped:
            for number in data.get(name, []):
                if (
                    not isinstance(number, str)
                    or len(number) != 12
                    or not number.startswith("+1")
                    or not number[1:].isascii()
                    or not number[1:].isdigit()
                    or number[2:5] not in npas
                ):
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
    except (ValueError, TypeError, KeyError):
        fail(422, "Valid authorized source, area codes, normalized endpoints and timestamp required")

    digest = hmac.new(service.db.identity_key, digestmod=hashlib.sha256)
    for piece in json.JSONEncoder(sort_keys=True, separators=(",", ":")).iterencode(data):
        digest.update(piece.encode())
    key = "registry-import:" + request_id
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        if not active_admin(service, s, p) or not service._coach_authority(s, p):
            fail(403, "Current administrator authority is required")
        stage = service._by_key(s, p, "registry_snapshots", key)
        if stage and stage.data.get("input_digest") != digest.hexdigest():
            fail(409, "Registry request identity already belongs to a different file")
        if stage and stage.data.get("status") == "published":
            return publication_receipt(stage)
        previous = [
            r
            for r in published_snapshots(s, p)
            if r.data["kind"] == kind and r.data.get("jurisdiction", "") == jurisdiction
        ]
        expected = [[r.id, r.version] for r in previous]
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
                    **{key: value.isoformat() for key, value in expiries.items()},
                    "refreshed_at": refreshed.isoformat(),
                    "published": False,
                    "status": "staging",
                    "input_digest": digest.hexdigest(),
                    "expected_publications": expected,
                    "chunks": {},
                    "synthetic": service.settings.simulation,
                },
                key=key,
                now=service.clock(),
            )
        stage_id, base_data = stage.id, doc(base) if base else None

    # The legacy checksum hashes the canonical compressed full-file envelope. Stream it
    # while storing independent chunks, so existing supplied checksums remain compatible.
    compressor, checksum = zlib.compressobj(9), hashlib.sha256()
    checksum.update(compressor.compress(b"{"))
    first, count = True, 0
    for npa in npas:
        bits = None
        if base_data:
            with service.db.transaction(False) as s:
                part = base_data.get("chunks", {}).get(npa)
                if part:
                    child = service.required(s, "registry_snapshots", part["id"], p)
                    if child.data.get("publication_id") != base_data["id"] or child.data.get("npa") != npa:
                        raise RegistryError("Changed source chunk identity")
                    bits = RegistryBitmap.load(
                        base64.b64decode(child.data["bitmap"]), part["checksum"]
                    ).regions[npa]
                elif base_data.get("bitmap"):
                    bits = RegistryBitmap.load(
                        base64.b64decode(base_data["bitmap"]), base_data["checksum"]
                    ).regions.get(npa)
                elif base_data.get("listed_numbers"):
                    bits = RegistryBitmap.build(
                        [v for v in base_data["listed_numbers"] if v[2:5] == npa]
                    ).regions.get(npa)
        added = grouped["add" if base_data else "numbers"].get(npa, set())
        removed = grouped["remove"].get(npa, set())
        if bits is None and not added:
            continue
        bits = bytearray(bits) if bits is not None else bytearray(1_250_000)
        for number in added:
            suffix = int(number[5:])
            bits[suffix >> 3] |= 1 << (suffix & 7)
        for number in removed:
            suffix = int(number[5:])
            bits[suffix >> 3] &= ~(1 << (suffix & 7))
        chunk = RegistryBitmap({npa: bits})
        encoded = json.dumps(npa).encode() + b": " + json.dumps(bytes(bits).hex()).encode()
        checksum.update(compressor.compress((b"" if first else b", ") + encoded))
        first = False
        payload = chunk.dump()
        part_hash = hashlib.sha256(payload).hexdigest()
        part_count = sum(value.bit_count() for value in bits)
        count += part_count
        with service.db.transaction() as s:
            service.db.authorize_stream(s, p)
            if not active_admin(service, s, p) or not service._coach_authority(s, p):
                fail(403, "Current administrator authority is required")
            stage = service.required(s, "registry_snapshots", stage_id, p)
            if stage.data.get("status") == "published":
                return publication_receipt(stage)
            child = service._by_key(s, p, "registry_snapshots", f"{stage_id}:{npa}")
            if not child:
                child = put(
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
            chunks = {
                **stage.data["chunks"],
                npa: {"id": child.id, "checksum": part_hash, "count": part_count},
            }
            update(stage, chunks=chunks, cursor=npa)
    checksum.update(compressor.compress(b"}"))
    checksum.update(compressor.flush())
    if data.get("checksum") and data["checksum"] != checksum.hexdigest():
        fail(422, "Registry checksum mismatch; prior publication retained")
    if data.get("count") is not None and data["count"] != count:
        fail(422, "Registry count mismatch; prior publication retained")
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        if not active_admin(service, s, p) or not service._coach_authority(s, p):
            fail(403, "Current administrator authority is required")
        stage = service.required(s, "registry_snapshots", stage_id, p)
        if stage.data.get("status") == "published":
            return publication_receipt(stage)
        current = [
            r
            for r in published_snapshots(s, p)
            if r.data["kind"] == kind and r.data.get("jurisdiction", "") == jurisdiction
        ]
        if sorted([[r.id, r.version] for r in current]) != sorted(stage.data["expected_publications"]):
            fail(409, "Published source changed while staging; review the new source")
        for row in current:
            update(row, published=False)
        update(
            stage,
            published=True,
            status="published",
            published_version=stage.version + 1,
            checksum=checksum.hexdigest(),
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
            {"checksum": checksum.hexdigest(), "count": count},
        )
        return publication_receipt(stage)


class RegistryBitmap:
    """Exact per-NPA suffix bitsets. Restricted to compliance, never scheduler features."""

    def __init__(self, regions):
        self.regions = regions

    @classmethod
    def build(cls, numbers):
        regions = {}
        for number in numbers:
            if len(number) != 12 or not number.startswith("+1") or not number[1:].isdigit():
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
        # Deterministic envelope permits checksums and atomic snapshot publication.
        payload = {npa: bytes(bits).hex() for npa, bits in sorted(self.regions.items())}
        return zlib.compress(json.dumps(payload, sort_keys=True).encode(), 9)

    @property
    def checksum(self):
        return hashlib.sha256(self.dump()).hexdigest()

    @classmethod
    def iter_regions(cls, payload, checksum):
        """Read the existing JSON/hex bitset format one NPA at a time.

        The legacy encrypted blob is already resident, but country-sized decoded
        JSON is never assembled. This strict reader accepts the format dump emits.
        """
        if hashlib.sha256(payload).hexdigest() != checksum:
            raise RegistryError("Registry checksum mismatch")
        inflater, buffer, tail, offset = zlib.decompressobj(), b"", b"", 0

        def read(size):
            nonlocal buffer, tail, offset
            while len(buffer) < size and not inflater.eof:
                block = tail or payload[offset : offset + 65536]
                if not tail:
                    offset += len(block)
                if not block:
                    raise RegistryError("Truncated registry stream")
                try:
                    buffer += inflater.decompress(block, 65536)
                except zlib.error as exc:
                    raise RegistryError("Invalid registry stream") from exc
                tail = inflater.unconsumed_tail
                if inflater.unused_data or inflater.eof and offset < len(payload):
                    raise RegistryError("Trailing registry stream")
            result, buffer = buffer[:size], buffer[size:]
            return result

        def token():
            value = read(1)
            while value and value in b" \t\r\n":
                value = read(1)
            return value

        if token() != b"{":
            raise RegistryError("Invalid registry envelope")
        current, seen = token(), set()
        if current != b"}":
            while True:
                npa = read(3).decode("ascii") if current == b'"' else ""
                if len(npa) != 3 or not npa.isdigit() or npa[0] not in "23456789" or npa in seen:
                    raise RegistryError("Invalid or duplicate registry NPA")
                if read(1) != b'"' or token() != b":" or token() != b'"':
                    raise RegistryError("Invalid registry member")
                try:
                    bits = bytearray.fromhex(read(2_500_000).decode("ascii"))
                except (ValueError, UnicodeError) as exc:
                    raise RegistryError("Invalid registry bitset") from exc
                if len(bits) != 1_250_000 or read(1) != b'"':
                    raise RegistryError("Invalid registry bitset length")
                seen.add(npa)
                yield npa, bits
                delimiter = token()
                if delimiter == b"}":
                    break
                if delimiter != b",":
                    raise RegistryError("Invalid registry delimiter")
                current = token()
        if token() or not inflater.eof:
            raise RegistryError("Trailing registry content")

    @classmethod
    def load(cls, payload, checksum):
        if hashlib.sha256(payload).hexdigest() != checksum:
            raise RegistryError("Registry checksum mismatch")
        inflater = zlib.decompressobj()
        try:
            decoded = inflater.decompress(payload, 50_000_001)
        except zlib.error as exc:
            raise RegistryError("Invalid compressed registry") from exc
        if len(decoded) > 50_000_000 or not inflater.eof or inflater.unused_data:
            raise RegistryError("Registry decompression limit or framing exceeded")
        try:
            raw = json.loads(decoded)
            result = {k: bytearray.fromhex(v) for k, v in raw.items()}
        except (ValueError, TypeError, AttributeError) as exc:
            raise RegistryError("Invalid registry bitmap") from exc
        if any(len(v) != 1_250_000 or not k.isdigit() or len(k) != 3 for k, v in result.items()):
            raise RegistryError("Invalid bitmap snapshot")
        return cls(result)

    def apply_changes(self, add, remove):
        result = RegistryBitmap({k: bytearray(v) for k, v in self.regions.items()})
        for number in add:
            bits = result.regions.setdefault(number[2:5], bytearray(1_250_000))
            suffix = int(number[5:])
            bits[suffix >> 3] |= 1 << (suffix & 7)
        for number in remove:
            if number[2:5] in result.regions:
                suffix = int(number[5:])
                result.regions[number[2:5]][suffix >> 3] &= ~(1 << (suffix & 7))
        return result
