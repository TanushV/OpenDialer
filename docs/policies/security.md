# Security and credential storage

OpenDialer 0.6 protects saved provider settings, personal Codex logins and managed-webhook sessions with authenticated encryption (Fernet). Desktop keys are created automatically in a private local file: `~/Library/Application Support/OpenDialer/Security/storage-key.json` on macOS, `%LOCALAPPDATA%/OpenDialer/Security/storage-key.json` on Windows. There are no OS credential-store requests or password popups. There is no plaintext credential fallback. Ordinary Settings responses report whether a key is configured without returning its value.

Existing 0.4/0.5 credential files convert atomically on first launch. Missing keys or damaged ciphertext stop access without overwriting the saved file. Back up the private key file as well as the encrypted files, or re-enter credentials after moving computers. Inaccessible 0.6.0/0.6.1 OS-store encrypted files are renamed with a unique `.keychain-….bak` suffix without accessing the OS store. Re-enter keys and re-import login/setup files to reconnect; business records are unchanged. Explicit server-key encrypted files migrate using that key and are never archived on a wrong/missing key. Encryption does not retroactively remove old plaintext backups or copies of imported setup/login files. Keep those private. Each independent local computer needs its own managed-webhook setup profile.

Linux/team deployments supply one persistent `OPENDIALER_SECRET_KEY_FILE` or `OPENDIALER_SECRET_KEY`, shared by API and worker processes. Generate a Fernet key once and store it separately from data backups. Never regenerate it on each restart. Docker Compose requires this value explicitly. Desktop users do not create or paste an encryption key.

The native Codex process receives a private temporary plaintext login file while it runs so its managed OAuth refresh works. Its refreshed credentials return to encrypted storage even when inference fails; normal exit removes the temporary directory. An abrupt OS/process termination can leave temporary files. Imported files remain user-owned and are not deleted by the app.

## Access and delivery

A local authority binds to loopback and requires a random launch token for local setup/session creation. The desktop passes that token over stdin. Connected sessions are expiring, database-revocable opaque bearer tokens stored as hashes; passwords use Argon2. Administrative routes and object access enforce roles and tenant/seller scope. HTTP and WebSocket browser origins are checked. Validation errors omit submitted input. Public research resolves and validates every destination, connects to the vetted public IP with the original TLS hostname, and repeats the check on redirects; environment proxies are disabled for this fetch. Failed password sign-ins are limited per source/account; successful sign-in clears only that account’s failure counter. A separate authority-wide limit bounds all password-hash work; horizontally scaled deployments should also configure a shared edge rate limit.

The shared webhook verifies Telnyx signatures, relays only control events and restricts delivery to the authenticated workspace. Audio goes directly from the computer to Telnyx. Provider keys are never sent to the shared webhook relay. A shared Telnyx API key can still grant account-wide permissions outside OpenDialer; relay isolation does not restrict that key. Use separate provider accounts or the hosted authority when callers must not have account-wide provider access.

## Boundaries

This is an internal-use release, not an independently certified production security product. Local file encryption protects a credential file copied without its key; someone who can read both can decrypt it. It does not protect against malicious software running as the same user. Business databases, exports, offline caches and backups are not encrypted by the application. Enable disk encryption, use separate OS accounts and protect backup destinations. Never import real records into simulation.

No call recording is implemented. Raw speech and helper text have a five-minute lifetime. Inference receives bounded relevant context; registry datasets are not model inputs. Live help uses the configured Cerebras route through OpenRouter with retention controls and provider verification. Provider contracts, consent and legal approval require their own review. Promoted models are validated JSON; serving does not unpickle files.

Connected production exposure uses HTTPS behind Caddy. Logs omit ordinary credential/transcript payloads. Signed updater artifacts are checked against the pinned public key; private release-signing material stays in CI secrets. Updater signing is separate from Apple notarization and Windows publisher signing, which are not configured for this preview.

## Suspected leak or lost device

Pause calls, revoke provider keys and Codex sessions at their providers, and revoke the affected managed-webhook user/session. Preserve relevant audit records and reconcile carrier effects before resuming. Deleting a local imported login is not provider-side revocation. Do not attach keys, setup bundles or databases to GitHub issues.
