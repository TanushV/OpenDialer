# Release and update

Keep the identifier `com.opendialer.desktop`. Bump the Python, desktop package/lock, Cargo/lock and Tauri versions together (`tests/test_distribution_contract.py` checks them).

Run **Signed desktop release** (`.github/workflows/release.yml`) from the Actions tab. On macOS and Windows it builds the UI, runs the distribution contract, freezes the authority and trainer sidecars with `scripts/build_sidecar.py`, signs the updater artifacts with `TAURI_SIGNING_PRIVATE_KEY` (kept in repository secrets, never in Git), and on macOS finishes scoped signing with `scripts/package_macos.py --build`. The draft job writes `latest.json` and checksums with `scripts/release_manifest.py` and creates a draft release with `docs/RELEASE_NOTES.md`.

Review the draft, install it on a test machine, then publish it. The installed updater follows GitHub's public latest manifest and verifies the pinned key. Users update from Settings → App updates; never uninstall or erase app data to update.

Trusted Apple Developer ID signing/notarization and Windows publisher signing are not configured; the preview is signed only for updater integrity.

**0.4.0 does not migrate 0.2/0.3 workspaces.** An updated installation opens a fresh workspace in `local-2/` (and `simulation-2/`); the old `local/` and `simulation/` folders stay untouched and only 0.3.3 can read them. Publish to existing installations only when starting fresh is acceptable.
