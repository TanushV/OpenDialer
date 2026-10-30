# Security and credential storage

OpenDialer 0.6 protects saved provider settings, personal Codex logins and managed-webhook sessions with authenticated encryption (Fernet). On macOS the encryption key is held in Keychain; on Windows it is held in Credential Manager. Only these native backends are accepted. A locked or unavailable store produces an error; there is no plaintext fallback. Ordinary Settings responses report whether a key is configured without returning its value.

Existing 0.4/0.5 credential files convert atomically on first launch. Missing keys or damaged ciphertext stop access without overwriting the saved file. Back up the OS credential store as well as the encrypted files, or re-enter credentials after moving computers. Encryption does not retroactively remove old plaintext backups or copies of imported setup/login files. Keep those private. Each independent local computer needs its own managed-webhook setup profile.

Linux/team deployments supply one persistent `OPENDIALER_SECRET_KEY_FILE` or `OPENDIALER_SECRET_KEY`, shared by API and worker processes. Generate a Fernet key once and store it separately from data backups. Never regenerate it on each restart. Docker Compose requires this value explicitly. Desktop users do not create or paste an encryption key.

The native Codex process receives a private temporary plaintext login file while it runs so its managed OAuth refresh works. Its refreshed credentials return to encrypted storage even when inference fails; normal exit removes the temporary directory. An abrupt OS/process termination can leave temporary files. Imported files remain user-owned and are not deleted by the app.

## Access and delivery

A local authority binds to loopback and requires a random launch token for local setup/session creation. The desktop passes that token over stdin. Connected sessions are expiring, database-revocable opaque bearer tokens stored as hashes; passwords use Argon2. Administrative routes and object access enforce roles and tenant/seller scope. HTTP and WebSocket browser origins are checked. Validation errors omit submitted input. Public research resolves and validates every destination, connects to the vetted public IP with the original TLS hostname, and repeats the check on redirects; environment proxies are disabled for this fetch. Password sign-in attempts are limited per source and per authority; horizontally scaled deployments should also configure a shared edge rate limit.

The shared webhook verifies Telnyx signatures, relays only control events and restricts delivery to the authenticated workspace. Audio goes directly from the computer to Telnyx. Provider keys are never sent to the shared webhook relay. A shared Telnyx API key can still grant account-wide permissions outside OpenDialer; relay isolation does not restrict that key. Use separate provider accounts or the hosted authority when callers must not have account-wide provider access.

## Boundaries

This is an internal-use release, not an independently certified production security product. OS credential storage reduces exposure from copied files; it cannot protect an unlocked account from every malicious process. Business databases, exports, offline caches and backups are not encrypted by the application. Enable disk encryption, use separate OS accounts and protect backup destinations. Never import real records into simulation.

No call recording is implemented. Raw speech and helper text have a five-minute lifetime. Inference receives bounded relevant context; registry datasets are not model inputs. Live help uses the configured Cerebras route through OpenRouter with retention controls and provider verification. Provider contracts, consent and legal approval require their own review. Promoted models are validated JSON; serving does not unpickle files.

Connected production exposure uses HTTPS behind Caddy. Logs omit ordinary credential/transcript payloads. Signed updater artifacts are checked against the pinned public key; private release-signing material stays in CI secrets. Updater signing is separate from Apple notarization and Windows publisher signing, which are not configured for this preview.

## Suspected leak or lost device

Pause calls, revoke provider keys and Codex sessions at their providers, and revoke the affected managed-webhook user/session. Preserve relevant audit records and reconcile carrier effects before resuming. Deleting a local imported login is not provider-side revocation. Do not attach keys, setup bundles or databases to GitHub issues.
