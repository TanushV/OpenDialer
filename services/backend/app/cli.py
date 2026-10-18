import argparse
import asyncio
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path

import uvicorn

from app.config import Settings
from app.main import create_app


def main():
    parser = argparse.ArgumentParser(description="OpenDialer local/shared authority")
    parser.add_argument("command", nargs="?", choices=["serve", "worker", "migrate"], default="serve")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--mode", choices=["local", "connected"], default=None)
    parser.add_argument("--simulation", action="store_true", default=None)
    parser.add_argument("--queue", action="append")
    args = parser.parse_args()
    settings = Settings(
        **{k: v for k, v in vars(args).items() if k in {"data_dir", "mode", "simulation"} and v is not None}
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not os.getenv("OPENDIALER_DATABASE_URL"):
        settings.database_url = "sqlite:///" + str((settings.data_dir / "opendialer.db").resolve())
    if not settings.secret_key:
        key_path = settings.data_dir / "authority-secret"
        if not key_path.exists():
            descriptor = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w") as key_file:
                key_file.write(secrets.token_urlsafe(48))
        settings.secret_key = key_path.read_text()
    if settings.mode == "local" and not settings.local_token:
        raise SystemExit(
            "Set OPENDIALER_LOCAL_TOKEN or launch through the desktop; no unauthenticated local API"
        )
    if settings.mode == "connected" and settings.database_url.startswith("sqlite"):
        raise SystemExit("Connected deployments require OPENDIALER_DATABASE_URL=postgresql+psycopg://...")
    clock = (lambda: datetime(2026, 10, 14, 18, 0, tzinfo=UTC)) if settings.simulation else None
    from app.migrate import migrate_database

    migrate_database(settings.database_url)
    if args.command == "migrate":
        return
    app = create_app(settings, clock=clock)
    if args.command == "worker":
        settings.worker_enabled = True  # Separate worker processes also own bounded automatic scheduling.

        async def run():
            import httpx

            async with httpx.AsyncClient(
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10)
            ) as http:
                app.state.service.coach_http = http
                from app.runtime import AuthorityRuntime

                runtime = AuthorityRuntime(app.state.service, queues=args.queue or settings.worker_queues)
                await runtime.start()
                try:
                    await asyncio.Event().wait()
                finally:
                    await runtime.close()
                    app.state.service.raw.clear()
                    app.state.db.engine.dispose()

        asyncio.run(run())
    else:
        host = args.host or ("127.0.0.1" if settings.mode == "local" else "0.0.0.0")  # noqa: S104 - connected deployment behind the private proxy
        if settings.mode == "local" and host != "127.0.0.1":
            raise SystemExit("Standalone authority must bind to loopback")
        uvicorn.run(app, host=host, port=args.port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
