"""One temporary CPU child; no authority, secrets, network clients or extra service."""

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

MAX_INPUT = 16 * 1024 * 1024
MAX_REPLY = 2 * 1024 * 1024
MAX_ARTIFACT = 64 * 1024 * 1024
MAX_SECONDS = 300


def child_command():
    if getattr(sys, "frozen", False):
        return [sys.executable, "--train-child"]
    return [sys.executable, "-m", "app.ml.worker"]


def child_environment():
    allowed = {"PATH", "SystemRoot", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL"}
    if getattr(sys, "frozen", False):
        # Same executable worker reuses PyInstaller's extracted runtime and libraries.
        allowed.update(k for k in os.environ if k.startswith("_PYI_"))
        allowed.update({"LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"})
    result = {k: v for k, v in os.environ.items() if k in allowed}
    result.update(
        {
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "VECLIB_MAXIMUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
        }
    )
    if not getattr(sys, "frozen", False):
        result["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    return result


def encode(rows, output_dir, baseline=None):
    if not isinstance(rows, list) or len(rows) > 20000:
        raise ValueError("Training row bound exceeded")
    encoded = json.dumps(
        {"rows": rows, "output_dir": str(output_dir), "baseline": baseline}, allow_nan=False
    ).encode()
    if len(encoded) > MAX_INPUT:
        raise ValueError("Training input byte bound exceeded")
    return encoded


def training_slot(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(root / ".training.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        raise RuntimeError("Training is already active or its storage is unavailable") from None
    return descriptor


async def exchange(process, request):
    async def send():
        try:
            process.stdin.write(request)
            await process.stdin.drain()
            process.stdin.close()
            await process.stdin.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass  # The child's exit status/output remains authoritative.

    async def receive():
        value = bytearray()
        while chunk := await process.stdout.read(65536):
            value.extend(chunk)
            if len(value) > MAX_REPLY:
                raise RuntimeError("Training response exceeded its bound")
        return bytes(value)

    async with asyncio.TaskGroup() as group:
        group.create_task(send())
        output = group.create_task(receive())
    await process.wait()
    if process.returncode:
        raise RuntimeError("Training child failed; candidate not published")
    return output.result()


async def reap(process):
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    process.stdin.close()
    # Drain after kill so a paused pipe transport cannot keep wait() pending.
    while await process.stdout.read(65536):
        pass
    await process.wait()


def checked_result(contents, output_dir):
    result = json.loads(contents)
    path = output_dir / "challenger.joblib"
    if not isinstance(result, dict) or result.get("artifact") != str(path) or path.is_symlink():
        raise ValueError("Invalid artifact identity")
    with path.open("rb") as stream:
        data = stream.read(MAX_ARTIFACT + 1)
    if len(data) > MAX_ARTIFACT or hashlib.sha256(data).hexdigest() != result.get("checksum"):
        raise ValueError("Invalid artifact checksum or size")
    return result


async def train_isolated(rows, output_dir, *, timeout_seconds=MAX_SECONDS, baseline=None):
    output_dir = Path(output_dir).resolve()
    request = await asyncio.to_thread(encode, rows, output_dir, baseline)
    slot = training_slot(output_dir.parent)
    process, keep, created = None, False, False
    try:
        output_dir.mkdir(mode=0o700)  # Never replace or clean an existing model directory.
        created = True
        flags = {"creationflags": subprocess.BELOW_NORMAL_PRIORITY_CLASS} if os.name == "nt" else {}
        spawning = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *child_command(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=child_environment(),
                **flags,
            )
        )
        try:
            process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            process = await spawning
            raise
        try:
            contents = await asyncio.wait_for(exchange(process, request), min(MAX_SECONDS, timeout_seconds))
            result = await asyncio.to_thread(checked_result, contents, output_dir)
        except TimeoutError:
            raise RuntimeError("Training timed out; candidate not published") from None
        except Exception:
            raise RuntimeError("Training failed validation; candidate not published") from None
        keep = True
        return result
    finally:
        try:
            if process:
                cleanup = asyncio.create_task(reap(process))
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
                    raise
        finally:
            os.close(slot)  # OS also releases the capacity lock if the authority exits.
            if created and not keep:
                await asyncio.to_thread(shutil.rmtree, output_dir, True)


def main():
    # Independent hard stop also bounds fitting if its parent crashes mid-computation.
    timer = threading.Timer(MAX_SECONDS, lambda: os._exit(124))
    timer.daemon = True
    timer.start()
    try:
        if os.name != "nt":
            os.nice(10)
        content = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(content) > MAX_INPUT:
            raise ValueError("Input bound exceeded")
        request = json.loads(content)
        if not isinstance(request["rows"], list) or len(request["rows"]) > 20000:
            raise ValueError("Row bound exceeded")
        from app.ml.training import train_challenger

        result = train_challenger(request["rows"], request["output_dir"], baseline=request.get("baseline"))
        result["training_runtime"] = {"pid": os.getpid(), "native_threads": 1, "priority": "low"}
        response = json.dumps(result, allow_nan=False).encode()
        if len(response) > MAX_REPLY:
            raise ValueError("Output bound exceeded")
        sys.stdout.buffer.write(response)
        sys.stdout.buffer.flush()
    except Exception:
        # Do not expose dataset content or library exception text in durable job errors.
        raise SystemExit(1) from None
    finally:
        timer.cancel()


if __name__ == "__main__":
    main()
