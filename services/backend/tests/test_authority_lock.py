"""One authority per data directory, desktop secrets over stdin, and durable key creation."""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
LOCAL_TOKEN = "stdin-local-token-" + "a" * 40
SECRET_KEY = "stdin-secret-key-" + "b" * 48
BUSY_EXIT = 75


def environment(**values):
    clean = {k: v for k, v in os.environ.items() if not k.startswith("OPENDIALER_")}
    clean["PYTHONPATH"] = str(BACKEND)
    clean.update(values)
    return clean


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def authority(directory, port, *extra, env, stdin=subprocess.DEVNULL):
    return subprocess.Popen(  # noqa: S603 - current interpreter, fixed module, owned temporary data
        [
            sys.executable,
            "-m",
            "app.cli",
            "serve",
            "--mode",
            "local",
            "--simulation",
            "--port",
            str(port),
            "--data-dir",
            str(directory),
            *extra,
        ],
        cwd=BACKEND,
        env=env,
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
                if response.status == 200:
                    return json.loads(response.read())
        except (OSError, urllib.error.URLError):
            pass
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
    if process.stderr:
        process.stderr.close()


def secrets_line(**overrides):
    return (json.dumps({"local_token": LOCAL_TOKEN, "secret_key": SECRET_KEY, **overrides}) + "\n").encode()


def test_second_local_authority_on_the_same_directory_refuses_with_a_clear_error(tmp_path):
    env = environment(OPENDIALER_LOCAL_TOKEN=LOCAL_TOKEN, OPENDIALER_SECRET_KEY=SECRET_KEY)
    first_port = free_port()
    first = authority(tmp_path, first_port, env=env)
    try:
        wait_healthy(first, first_port)
        second = authority(tmp_path, free_port(), env=env)
        try:
            _, error = second.communicate(timeout=60)
        finally:
            stop(second)
        assert second.returncode == BUSY_EXIT, error.decode()[-800:]
        assert b"Another OpenDialer authority is already using" in error
        assert str(tmp_path).encode() in error
        assert first.poll() is None, "The running authority must be unaffected"
        wait_healthy(first, first_port, timeout=5)
    finally:
        stop(first)
    # The lock belongs to the process lifetime; once it exits the directory is usable again.
    third_port = free_port()
    third = authority(tmp_path, third_port, env=env)
    try:
        wait_healthy(third, third_port)
    finally:
        stop(third)


def test_desktop_secrets_arrive_once_on_stdin_and_override_inherited_environment(tmp_path):
    port = free_port()
    inherited = "inherited-environment-token-" + "c" * 40
    process = authority(
        tmp_path,
        port,
        "--secrets-stdin",
        env=environment(OPENDIALER_LOCAL_TOKEN=inherited),
        stdin=subprocess.PIPE,
    )
    try:
        process.stdin.write(secrets_line())
        process.stdin.flush()
        wait_healthy(process, port)
        # 409 means the stdin nonce authenticated and setup is still required.
        assert local_session_status(port, LOCAL_TOKEN) == 409
        assert local_session_status(port, inherited) == 401
        assert not (tmp_path / "authority-secret").exists(), "The desktop key must not be replaced"
    finally:
        if process.stdin:
            process.stdin.close()
        stop(process)


def test_launcher_loss_stops_the_stdin_authority_and_releases_its_lock(tmp_path):
    port = free_port()
    process = authority(tmp_path, port, "--secrets-stdin", env=environment(), stdin=subprocess.PIPE)
    try:
        process.stdin.write(secrets_line())
        process.stdin.flush()
        wait_healthy(process, port)
        process.stdin.close()  # Equivalent to the desktop process disappearing.
        process.wait(timeout=45)
    finally:
        stop(process)
    from app.cli import authority_lock

    with authority_lock(tmp_path):
        pass


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not json\n",
        secrets_line(local_token=""),
        secrets_line(extra="unexpected"),
        secrets_line(secret_key="short"),
        b'{"local_token": "' + b"x" * 20000 + b'"}\n',
    ],
)
def test_missing_or_malformed_stdin_secrets_refuse_to_start(tmp_path, payload):
    process = authority(tmp_path, free_port(), "--secrets-stdin", env=environment(), stdin=subprocess.PIPE)
    try:
        _, error = process.communicate(payload, timeout=60)
    finally:
        stop(process)
    assert process.returncode not in (0, None)
    assert b"secrets" in error.lower()
    assert not (tmp_path / "opendialer.db").exists(), "No state may be created without valid secrets"


def run_cli(monkeypatch, directory, *extra):
    from app.cli import main

    for name in list(os.environ):
        if name.startswith("OPENDIALER_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("OPENDIALER_LOCAL_TOKEN", LOCAL_TOKEN)
    monkeypatch.setattr(
        sys,
        "argv",
        ["opendialer", "migrate", "--mode", "local", "--simulation", "--data-dir", str(directory)],
    )
    main()


def test_authority_secret_is_published_atomically_and_reused(tmp_path, monkeypatch):
    run_cli(monkeypatch, tmp_path)
    key = tmp_path / "authority-secret"
    value = key.read_text()
    assert len(value) >= 32
    if os.name != "nt":
        assert key.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.tmp")) and not list(tmp_path.glob(".*.tmp"))
    run_cli(monkeypatch, tmp_path)
    assert key.read_text() == value


def test_empty_authority_secret_is_rejected_not_accepted_or_replaced(tmp_path, monkeypatch):
    key = tmp_path / "authority-secret"
    key.write_text("")
    with pytest.raises(SystemExit) as refusal:
        run_cli(monkeypatch, tmp_path)
    assert "authority-secret" in str(refusal.value.code)
    assert "empty or damaged" in str(refusal.value.code)
    assert key.read_text() == ""
    assert not (tmp_path / "opendialer.db").exists()


def test_missing_authority_secret_is_not_reminted_over_existing_data(tmp_path, monkeypatch):
    (tmp_path / "opendialer.db").write_bytes(b"")
    with pytest.raises(SystemExit) as refusal:
        run_cli(monkeypatch, tmp_path)
    assert "did not create a new key" in str(refusal.value.code)
    assert not (tmp_path / "authority-secret").exists()


def test_interrupted_key_creation_leftovers_do_not_block_a_fresh_directory(tmp_path):
    from app.cli import load_or_create_authority_secret

    (tmp_path / ".authority-secret.deadbeef.tmp").write_text("partial")
    value = load_or_create_authority_secret(tmp_path)
    assert (tmp_path / "authority-secret").read_text() == value
    assert load_or_create_authority_secret(tmp_path) == value


def test_authority_lock_is_exclusive_within_and_across_processes(tmp_path):
    from app.cli import AuthorityBusy, authority_lock

    with authority_lock(tmp_path):
        with pytest.raises(AuthorityBusy):
            with authority_lock(tmp_path):
                pass
    with authority_lock(tmp_path):
        pass
