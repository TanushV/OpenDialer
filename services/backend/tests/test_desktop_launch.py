"""The desktop launch contract: the local token over stdin, launcher loss and one authority per folder."""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
# The desktop shell sends two UUIDs; anything 32-4096 printable ASCII characters is accepted.
TOKEN = "0f8e2b1c-6d3a-4e5f-9a7b-1c2d3e4f5a6b" + "7c8d9e0f-1a2b-4c3d-8e5f-6a7b8c9d0e1f"
BUSY_EXIT = 75


def environment(**values):
    clean = {k: v for k, v in os.environ.items() if not k.startswith("OPENDIALER_")}
    return {**clean, "PYTHONPATH": str(BACKEND), **values}


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def authority(directory, port, *extra, env=None, stdin=subprocess.DEVNULL):
    command = [sys.executable, "-m", "app.cli", "serve", "--mode", "local", "--simulation"]
    return subprocess.Popen(  # noqa: S603 - current interpreter, fixed module, owned temporary data
        [*command, "--port", str(port), "--data-dir", str(directory), *extra],
        cwd=BACKEND,
        env=env or environment(),
        stdin=stdin,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def wait_healthy(process, port, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"authority exited early: {process.stderr.read().decode()[-800:]}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:  # noqa: S310 - owned loopback fixture
                return json.loads(response.read())
        except OSError:
            time.sleep(0.2)
    raise AssertionError("authority did not become healthy")


def local_session_status(port, token):
    request = urllib.request.Request(  # noqa: S310 - owned loopback fixture
        f"http://127.0.0.1:{port}/v1/local/session",
        data=b"{}",
        method="POST",
        headers={"X-Local-Token": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - owned loopback fixture
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def stop(process):
    if process.poll() is None:
        process.kill()
    process.wait(timeout=30)
    process.stderr.close()


def secrets(token=TOKEN):
    return (json.dumps({"local_token": token}) + "\n").encode()


def test_stdin_token_is_the_only_credential_and_launcher_loss_stops_the_authority(tmp_path):
    port = free_port()
    # A stray environment token must not open the authority the desktop launched.
    env = environment(OPENDIALER_LOCAL_TOKEN="environment-token-" + "x" * 32)
    process = authority(tmp_path, port, "--secrets-stdin", env=env, stdin=subprocess.PIPE)
    try:
        process.stdin.write(secrets())
        process.stdin.flush()
        health = wait_healthy(process, port)
        assert health["mode"] == "local" and health["initialized"] is False
        # 409 "setup required" means the token was accepted; 401 means it was not.
        assert local_session_status(port, TOKEN) == 409
        assert local_session_status(port, "environment-token-" + "x" * 32) == 401
        assert local_session_status(port, "wrong-" + "y" * 40) == 401
        process.stdin.close()  # The desktop exited or crashed: its only writer is gone.
        process.wait(timeout=30)
        assert process.returncode is not None
    finally:
        stop(process)


def test_malformed_launch_secrets_are_refused_with_a_reason(tmp_path):
    for line, reason in (
        (secrets("short"), b"local token on standard input is missing or invalid"),
        (b"not json\n", b"not valid JSON"),
        (b'{"local_token": "' + TOKEN.encode() + b'"}', b"missing or exceed their bound"),
    ):
        process = authority(tmp_path, free_port(), "--secrets-stdin", stdin=subprocess.PIPE)
        try:
            _, error = process.communicate(line, timeout=60)
        finally:
            stop(process)
        assert process.returncode != 0 and reason in error, error.decode()[-400:]


def test_a_second_authority_on_the_same_folder_refuses_and_the_first_keeps_serving(tmp_path):
    env = environment(OPENDIALER_LOCAL_TOKEN=TOKEN)
    port = free_port()
    first = authority(tmp_path, port, env=env)
    try:
        wait_healthy(first, port)
        second = authority(tmp_path, free_port(), env=env)
        try:
            _, error = second.communicate(timeout=60)
        finally:
            stop(second)
        assert second.returncode == BUSY_EXIT, error.decode()[-800:]
        assert b"Another OpenDialer authority is already using" in error
        assert first.poll() is None
        wait_healthy(first, port, timeout=5)
    finally:
        stop(first)
    # The lock lives with the process: once it exits, the folder opens again.
    port = free_port()
    third = authority(tmp_path, port, env=env)
    try:
        wait_healthy(third, port)
    finally:
        stop(third)
