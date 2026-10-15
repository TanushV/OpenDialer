"""Authenticated restore reconciles the append-only opt-out journal before resuming work."""

import argparse
import getpass
from urllib.parse import urlsplit

import httpx

parser = argparse.ArgumentParser()
parser.add_argument("server")
parser.add_argument("backup_id")
args = parser.parse_args()
url = urlsplit(args.server)
if url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost"}):
    raise SystemExit("Use HTTPS or a local loopback authority")
if url.username or url.password:
    raise SystemExit("Embedded URL credentials are not permitted")
token = getpass.getpass("Administrator session token: ")
response = httpx.post(
    args.server.rstrip("/") + "/v1/admin/restore",
    json={"backup_id": args.backup_id},
    headers={"Authorization": "Bearer " + token},
    timeout=60,
)
response.raise_for_status()
print(response.json())
