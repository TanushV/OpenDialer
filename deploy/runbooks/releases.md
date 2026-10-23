# Release and update

Keep the identifier `com.opendialer.desktop`. Bump the Python, desktop package/lock, Cargo/lock and Tauri versions together (`tests/test_distribution_contract.py` checks them).

Run **Signed desktop release** (`.github/workflows/release.yml`) from the Actions tab. On macOS and Windows it builds the UI, runs the distribution contract, freezes the authority and trainer sidecars with `scripts/build_sidecar.py`, signs the updater artifacts with `TAURI_SIGNING_PRIVATE_KEY` (kept in repository secrets, never in Git), and on macOS finishes scoped signing with `scripts/package_macos.py --build`. The draft job writes `latest.json` and checksums with `scripts/release_manifest.py` and creates a draft release with `docs/RELEASE_NOTES.md`.

Review the draft, install it on a test machine, then publish it. The installed updater follows GitHub's public latest manifest and verifies the pinned key. Users update from Settings → App updates; never uninstall or erase app data to update.

Trusted Apple Developer ID signing/notarization and Windows publisher signing are not configured; the preview is signed only for updater integrity.

**Do not publish the lean build to existing 0.2/0.3 installations.** It reads only fresh, unencrypted stores; an installed 0.2/0.3 workspace would not open after the update. Ship it to new installations, or plan an explicit export/import first.
