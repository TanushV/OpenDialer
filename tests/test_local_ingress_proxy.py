"""The shipped local-host recipe exposes only carrier ingress, never administration."""

import os
import shutil
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

RECIPE = Path(__file__).resolve().parents[1] / "deploy/Caddyfile.local-ingress"


def test_local_ingress_recipe_has_a_closed_fallback_and_no_administration():
    text = RECIPE.read_text()
    assert "path /webhooks/telnyx" in text and "method POST" in text
    assert "^/webhooks/ingress-check/[a-f0-9]{32}$" in text and "method GET" in text
    assert 'respond "Not found" 404' in text
    assert "127.0.0.1:{$OPENDIALER_FORWARD_PORT:18765}" in text
    assert "/v1/" not in text and "api:8765" not in text


@pytest.mark.skipif(
    not os.getenv("OPENDIALER_CADDY_IMAGE"), reason="Explicit disposable Caddy image required"
)
def test_actual_caddy_route_and_method_boundary(tmp_path):
    # Only the public site address changes. The actual shipped loopback upstream
    # is an in-container sentinel that returns 209 for every forwarded request.
    path = tmp_path / "Caddyfile"
    path.write_text(RECIPE.read_text() + '\nhttp://127.0.0.1:18765 {\n respond "forwarded" 209\n}\n')
    docker = shutil.which("docker")
    assert docker, "Docker is required for the explicit ingress acceptance"
    name = "opendialer-ingress-test-" + uuid4().hex
    image = os.environ["OPENDIALER_CADDY_IMAGE"]
    identity = subprocess.check_output(  # noqa: S603 - exact disposable Docker identity, no shell
        [
            docker,
            "create",
            "--name",
            name,
            "--label",
            "opendialer.task=completion-ingress",
            "--tmpfs",
            "/config",
            "--tmpfs",
            "/data",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "NET_BIND_SERVICE",  # The official binary carries this file capability.
            "-p",
            "127.0.0.1::18080",
            "-e",
            "OPENDIALER_INGRESS_DOMAIN=http://:18080",
            image,
        ],
        text=True,
    ).strip()
    try:
        subprocess.run(  # noqa: S603 - fixed argument array; task-owned container only
            [docker, "cp", str(path), identity + ":/etc/caddy/Caddyfile"], check=True
        )
        subprocess.run(  # noqa: S603 - fixed argument array; task-owned container only
            [docker, "start", identity], check=True, capture_output=True
        )
        address = subprocess.check_output(  # noqa: S603
            [docker, "port", identity, "18080/tcp"], text=True
        ).strip()
        with httpx.Client(base_url="http://" + address, timeout=2, trust_env=False) as client:
            for _ in range(40):
                try:
                    if client.get("/health").status_code == 404:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            cases = [("POST", "/webhooks/telnyx", 209), ("GET", "/webhooks/ingress-check/" + "a" * 32, 209)]
            for path in [
                "/health",
                "/v1/workspace",
                "/v1/admin/settings",
                "/webhooks/other",
                "/webhooks/telnyx/extra",
                "/webhooks/ingress-check/short",
                "/webhooks/ingress-check/" + "a" * 32 + "/extra",
            ]:
                cases.extend((method, path, 404) for method in ["GET", "POST", "PUT", "DELETE", "OPTIONS"])
            cases.extend([(method, "/webhooks/telnyx", 404) for method in ["GET", "PUT", "HEAD", "OPTIONS"]])
            cases.extend(
                [(method, "/webhooks/ingress-check/" + "a" * 32, 404) for method in ["POST", "HEAD"]]
            )
            for method, path, expected in cases:
                response = client.request(method, path)
                assert response.status_code == expected, (method, path, response.status_code)
    except Exception:
        print(subprocess.check_output([docker, "logs", identity], text=True, stderr=subprocess.STDOUT))  # noqa: S603
        raise
    finally:
        # Exact ID belongs to this test; never stop a user Caddy or occupied port.
        subprocess.run(  # noqa: S603 - fixed argument array; task-owned container only
            [docker, "rm", "--force", identity], check=True, capture_output=True
        )
