# Install OpenDialer

[Back to README](../README.md) · [Requirements](REQUIREMENTS.md) · [First-time setup](GETTING_STARTED.md)

## Mac

1. Open [the latest release](https://github.com/TanushV/OpenDialer/releases/latest) and download the `aarch64.dmg` file.
2. Open the DMG and drag **OpenDialer** into **Applications**.
3. Open OpenDialer from Applications and allow microphone access when requested.
4. Continue with [workspace setup](GETTING_STARTED.md).

The current Mac download is for **Apple Silicon (M-series), macOS 14 or newer**. It will not run on an Intel Mac.

The preview is not Apple-notarized. If macOS blocks it as an unidentified developer, verify you downloaded
from this repository and follow [Apple's instructions](https://support.apple.com/en-us/102445):
after attempting to open it, use **System Settings → Privacy & Security → Open Anyway** if available.
Do not disable Gatekeeper globally. If macOS reports malware or the file is damaged, stop and report the exact message.

## Windows

1. Open [the latest release](https://github.com/TanushV/OpenDialer/releases/latest) and download the `x64-setup.exe` file.
2. Run the installer and follow its prompts. It includes an offline WebView2 installer.
3. Open OpenDialer, allow microphone access and continue with [workspace setup](GETTING_STARTED.md).

Use an Intel/AMD 64-bit PC; Windows 11 is recommended. Windows 10 compatibility is not clean-machine certified.
The `.msi` is an alternative for managed installation; most users should choose the EXE.
The preview has no Windows publisher certificate, so Windows or organizational policy may flag it as unrecognized.
Check the repository/download with your administrator if blocked. No Windows ARM installer is published.

## Update an existing installation

1. Open **Settings → More settings → Workspace and app updates**.
2. Click **Check for updates**, then **Install signed update** when offered.
3. Quit OpenDialer (**OpenDialer → Quit OpenDialer** on a Mac) and open it again.

Your workspace, leads and keys are kept. Do not uninstall or delete workspace data to update.
The updater checks the repository's pinned signing key; that verifies the download, separately from Apple
notarization or Windows publisher trust. Versions before 0.7 show the same steps under **Settings → App updates**.

**0.7.1** is the current version: one Markdown script, AI research and a script for every lead before calling,
a one-page Settings and a plain practice list. [What's new](RELEASE_NOTES.md). After updating from 0.6.x, paste
your script again on the **Script** page in the new format ([example](examples/script.md)); the
[setup guide](GETTING_STARTED.md#4-add-your-script) explains it. Updates from 0.4 to 0.6 keep your workspace.
Upgrading from 0.6.0/0.6.1 may ask you to re-enter provider keys and re-import setup or login files; the old
encrypted files are kept for recovery.

**Upgrading from 0.2/0.3:** 0.4 and later start fresh workspaces and leave the old data folders untouched;
they do not migrate old workspaces. Keep backups and consult [recovery](../deploy/runbooks/recovery.md) before switching.

## Which files do I need?

Choose **one** installer: Mac DMG or Windows setup EXE. You do not need the source ZIP,
`.sig`, `latest.json` or Mac `.tar.gz` for a normal first install. The latter files support updates.
`SHA256SUMS.txt` on the release page lists installer checksums for manual verification.

If installation fails, [open an issue](https://github.com/TanushV/OpenDialer/issues) with your OS version,
CPU type, app version and exact error. Do not attach API keys, workspace databases or prospect data.
