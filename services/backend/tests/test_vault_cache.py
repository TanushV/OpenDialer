"""Provider configuration reads are cached by file identity, never served stale."""

from app.security import Vault


def counting(vault):
    calls = []
    original = vault.cipher.decrypt

    def decrypt(token):
        calls.append(1)
        return original(token)

    vault.cipher.decrypt = decrypt
    return calls


def test_repeated_reads_decrypt_once_and_return_independent_copies(tmp_path):
    vault = Vault(tmp_path, "vault-cache-key")
    vault.write("t", {"telnyx_api_key": "KEY", "nested": {"a": 1}})
    calls = counting(vault)
    first = vault.read("t")
    first["nested"]["a"] = 99  # A caller mutating its copy cannot poison the cache.
    second = vault.read("t")
    assert second["nested"]["a"] == 1
    assert len(calls) == 1, "Unchanged configuration must not be re-decrypted on every read"


def test_writes_and_other_processes_invalidate_the_cache(tmp_path):
    vault, other = Vault(tmp_path, "vault-cache-key"), Vault(tmp_path, "vault-cache-key")
    vault.write("t", {"mode": "one"})
    assert vault.read("t")["mode"] == "one"
    vault.write("t", {"mode": "two"})
    assert vault.read("t")["mode"] == "two"
    other.write("t", {"mode": "three"})  # Another authority process replaces the file atomically.
    assert vault.read("t")["mode"] == "three"
    assert Vault(tmp_path, "vault-cache-key").read("missing") == {}
