# OpenDialer 0.6.0

- Saved provider credentials, Codex logins and webhook sessions are encrypted using the OS credential store. Existing 0.4/0.5 settings convert automatically without losing workspace data. Hosted deployments must configure a persistent encryption key before updating.
- Local callers can import one per-PC setup file for the shared webhook. No tunnel or terminal command is needed. Speech goes directly to Telnyx/Soniox, with two-second coaching windows.
- Import a Codex login in Settings with managed refresh. Calling setup and number purchases are simpler, with explicit quote approval and count/spending limits.
- Plan number capacity from named lead lists by state. Suggested metro area codes reflect list geography; owned capacity is shared and existing lead-to-number assignments are preserved.
- Reject untrusted browser origins, omit submitted secrets from validation errors, rate-limit password sign-in, and prevent public research from reaching private networks or following DNS rebinding.
- Update vulnerable Python build and Rust TLS dependencies.

Updates from 0.4.x and 0.5.x retain the workspace. Versions 0.2/0.3 start a fresh workspace; old data stays untouched in its previous folder and is not migrated.

Updater signatures verify artifact integrity. Apple notarization, Windows publisher signing, clean-machine installation and live carrier/audio behavior remain separate gates. This release improves security; it is not an independent penetration-test certification. Business databases and exports still rely on OS disk and backup protection.
