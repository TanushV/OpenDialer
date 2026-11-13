"""Launch the installed Linux desktop with an empty profile under Xvfb."""

import argparse
import json
import os
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path


def authority_ports(desktop_pid):
    # Any Tauri worker thread can launch the sidecar. Only inspect children of this desktop.
    for children in Path(f"/proc/{desktop_pid}/task").glob("*/children"):
        try:
            for pid in children.read_text().split():
                args = Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")
                if Path(args[0]).name == "opendialer-backend" and "--port" in args:
                    yield int(args[args.index("--port") + 1])
        except (FileNotFoundError, ProcessLookupError):
            continue


def wait_for_desktop(process):
    deadline = time.monotonic() + 90
    while process.poll() is None and time.monotonic() < deadline:
        for port in authority_ports(process.pid):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:  # noqa: S310 - owned loopback fixture
                    health = json.load(response)
            except OSError:
                continue
            if health["mode"] == "local" and health["initialized"] is False:
                windows = subprocess.run(  # noqa: S603 - fixed X11 window probe for the launched desktop
                    [
                        "/usr/bin/xdotool",
                        "search",
                        "--all",
                        "--onlyvisible",
                        "--pid",
                        str(process.pid),
                        "--name",
                        "^OpenDialer$",
                    ],
                    capture_output=True,
                    timeout=5,
                )
                if windows.returncode == 0 and windows.stdout.strip():
                    return
        time.sleep(0.25)
    raise SystemExit(
        f"Linux desktop did not open its window and start its local workspace (exit: {process.poll()})"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("desktop", type=Path)
    desktop = parser.parse_args().desktop.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="opendialer-linux-") as scratch:
        root = Path(scratch)
        env = {key: value for key, value in os.environ.items() if not key.startswith("OPENDIALER_")}
        env.update(HOME=scratch, XDG_DATA_HOME=str(root / "data"), XDG_CONFIG_HOME=str(root / "config"))
        with (root / "desktop.log").open("w+") as log:
            process = subprocess.Popen([desktop], env=env, stdout=log, stderr=log)  # noqa: S603 - installed test package
            try:
                wait_for_desktop(process)
                print("Linux desktop loaded its webview and started the bundled local authority.")
            except SystemExit:
                log.seek(0)
                print(log.read()[-4000:])
                raise
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    main()
