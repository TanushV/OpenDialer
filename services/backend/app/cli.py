import argparse
import asyncio
import json
import os
import signal
import sys
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.config import Settings

AUTHORITY_BUSY_EXIT = 75  # EX_TEMPFAIL; the desktop maps it to an actionable message.
SECRET_LINE_LIMIT = 16384
SECRET_WAIT_SECONDS = 30


class AuthorityBusy(RuntimeError):
    pass


@contextmanager
def authority_lock(directory):
    """Hold an exclusive, non-inheritable lock on one authority data directory."""
    path = Path(directory) / "authority.lock"
    if path.is_symlink():
        raise AuthorityBusy(f"The authority lock at {path} cannot be a symbolic link")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise AuthorityBusy(
                f"Another OpenDialer authority is already using {Path(directory)}. "
                "Quit the other OpenDialer window or process, then retry; no data was changed."
            ) from None
        yield
    finally:
        os.close(descriptor)  # Closing the only descriptor releases the lock.


def read_launch_secrets(stream, timeout=SECRET_WAIT_SECONDS):
    """Read the desktop's one JSON line; further stdin bytes are never parsed."""
    received = {}

    def read():
        try:
            received["line"] = stream.readline(SECRET_LINE_LIMIT + 1)
        except (OSError, ValueError):
            received["line"] = b""

    reader = threading.Thread(target=read, name="launch-secrets", daemon=True)
    reader.start()
    reader.join(timeout)
    if reader.is_alive():
        raise SystemExit("The desktop launcher did not provide authority secrets on standard input")
    line = received.get("line", b"")
    if not line.endswith(b"\n") or len(line) > SECRET_LINE_LIMIT:
        raise SystemExit("Authority secrets on standard input are missing or exceed their bound")
    try:
        payload = json.loads(line)
    except ValueError:
        raise SystemExit("Authority secrets on standard input are not valid JSON") from None
    token = payload.get("local_token") if isinstance(payload, dict) else None
    if (
        not isinstance(token, str)
        or not 32 <= len(token) <= 4096
        or not token.isascii()
        or not token.isprintable()
    ):
        raise SystemExit("The local token on standard input is missing or invalid")
    return {"local_token": token}


def _launcher_lost():
    # Graceful shutdown through the server's own signal handling, then a bounded hard stop.
    stopper = threading.Timer(20, os._exit, args=(0,))
    stopper.daemon = True
    stopper.start()
    signal.raise_signal(signal.SIGTERM)


def watch_launcher(stream, on_lost=_launcher_lost):
    """The desktop holds the only writer; EOF means it exited, even after a crash."""

    def watch():
        try:
            while stream.read(4096):
                pass  # Discard; the secret line was the only accepted input.
        except (OSError, ValueError):
            pass
        on_lost()

    watcher = threading.Thread(target=watch, name="launcher-liveness", daemon=True)
    watcher.start()
    return watcher


@contextmanager
def _no_lock():
    yield


def main():
    parser = argparse.ArgumentParser(description="OpenDialer local/shared authority")
    parser.add_argument("command", nargs="?", choices=["serve", "worker", "migrate"], default="serve")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--mode", choices=["local", "connected"], default=None)
    parser.add_argument("--simulation", action="store_true", default=None)
    parser.add_argument("--queue", action="append")
    parser.add_argument(
        "--secrets-stdin",
        action="store_true",
        help="Read local_token from one JSON line on stdin (desktop launcher)",
    )
    args = parser.parse_args()
    launch = read_launch_secrets(sys.stdin.buffer) if args.secrets_stdin else None
    overrides = {
        k: v for k, v in vars(args).items() if k in {"data_dir", "mode", "simulation"} and v is not None
    }
    if launch:
        # Launcher-provided secrets win, and are never passed on through the environment.
        os.environ.pop("OPENDIALER_LOCAL_TOKEN", None)
        overrides.update(launch)
    settings = Settings(**overrides)
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        lock = authority_lock(settings.data_dir) if settings.mode == "local" else _no_lock()
        with lock:
            run(args, settings, launch is not None)
    except AuthorityBusy as busy:
        print(busy, file=sys.stderr)
        raise SystemExit(AUTHORITY_BUSY_EXIT) from None


def run(args, settings, watch_stdin):
    if not os.getenv("OPENDIALER_DATABASE_URL"):
        settings.database_url = "sqlite:///" + str((settings.data_dir / "opendialer.db").resolve())
    if settings.mode == "local" and not settings.local_token:
        raise SystemExit(
            "Set OPENDIALER_LOCAL_TOKEN, use --secrets-stdin, or launch through the desktop; "
            "no unauthenticated local API"
        )
    if settings.mode == "connected" and settings.database_url.startswith("sqlite"):
        raise SystemExit("Connected deployments require OPENDIALER_DATABASE_URL=postgresql+psycopg://...")
    if settings.mode == "connected" and getattr(sys, "frozen", False):
        # The desktop sidecar ships without PostgreSQL drivers; it only ever runs local mode.
        raise SystemExit(
            "This desktop authority runs local mode only; run connected mode from the server image"
        )
    import uvicorn

    from app.main import create_app

    clock = (lambda: datetime(2026, 10, 14, 18, 0, tzinfo=UTC)) if settings.simulation else None
    app = create_app(settings, clock=clock)
    if args.command == "migrate":
        app.state.db.engine.dispose()
        return
    if watch_stdin:
        watch_launcher(sys.stdin.buffer)  # After migrations, so launcher loss never interrupts them.
    from app import priority

    priority.settle_heap()
    if args.command == "worker":
        settings.worker_enabled = True  # Separate worker processes also own bounded automatic scheduling.

        async def work():
            from app.runtime import authority_lifetime

            async with authority_lifetime(app.state.service, queues=args.queue or settings.worker_queues):
                await asyncio.Event().wait()

        asyncio.run(work())
    else:
        host = args.host or ("127.0.0.1" if settings.mode == "local" else "0.0.0.0")  # noqa: S104 - connected deployment behind the private proxy
        if settings.mode == "local" and host != "127.0.0.1":
            raise SystemExit("Standalone authority must bind to loopback")
        uvicorn.run(app, host=host, port=args.port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
