import os
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    import certifi

    # Bundled Python may retain a build-machine OpenSSL CA path. HTTPS clients use
    # certifi already; stdlib/WebSocket TLS needs the same roots, with verification on.
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/backend"))
if __name__ == "__main__":
    from app.cli import main

    main()
