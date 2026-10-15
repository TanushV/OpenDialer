import hashlib
import json
import zlib


class RegistryError(ValueError):
    pass


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
        if len(number) != 12 or not number.startswith("+1"):
            return False
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
    def load(cls, payload, checksum):
        if hashlib.sha256(payload).hexdigest() != checksum:
            raise RegistryError("Registry checksum mismatch")
        try:
            raw = json.loads(zlib.decompress(payload, max_length=50_000_000))
        except TypeError:
            raw = json.loads(zlib.decompress(payload))
        result = {k: bytearray.fromhex(v) for k, v in raw.items()}
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
