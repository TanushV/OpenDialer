# Release and update

Keep the stable identifier `com.opendialer.desktop`. Bump Python, desktop package/lock, Cargo/lock and Tauri versions together. Complete the acceptance matrix and preserve an installed baseline with a human note and suppression. Freeze the backend with `scripts/build_sidecar.py`; the release must not contain a shell wrapper or depend on a source checkout/Python installation.

Keep `TAURI_SIGNING_PRIVATE_KEY` and its password outside Git. The repository Actions secret store is configured; the pinned public key is in Tauri config. Build native packages on the corresponding OS. `.app.tar.gz.sig`/Windows/Linux signatures and the updater JSON must refer to the exact uploaded bytes. Publish a reviewed release only after verification; draft CI packages do not update users. The configured latest endpoint requires a published non-prerelease release. Developer previews must clearly disclose missing platform/provider certification.

For macOS public distribution, configure a real Apple Developer ID certificate, hardened runtime entitlements and notarization credentials, then validate a quarantined clean install. Updater integrity signing is separate from Apple trust. Windows signing and Linux WebRTC/headset certification have independent gates. Never claim these are passed because compilation succeeds.

Install once. Thereafter use Settings → App updates, verify the signed offered version, install it in place, quit/reopen, and check the saved workspace IDs, human note, suppressions, database integrity and new version. A failed signature must not replace the app. Do not uninstall/reinstall or reset data for an update.

Release artifacts include checksums, dependency notices, source revision and the acceptance report. Dependency/security CI, code review and reproducible test output accompany each release. Preserve updater key recovery material securely; losing it requires a separately planned trusted rotation, not silently changing the public key.
