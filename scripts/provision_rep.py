"""Issue a private OpenDialer profile on an explicitly named Supabase relay."""

import argparse
import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from uuid import UUID, uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
PROFILE_KEYS = {
    "telnyx_api_key": "OPENDIALER_PROFILE_TELNYX_KEY",
    "hubspot_service_key": "OPENDIALER_PROFILE_HUBSPOT_KEY",
    "openrouter_api_key": "OPENDIALER_PROFILE_OPENROUTER_KEY",
    "treg_token": "OPENDIALER_PROFILE_TREG_TOKEN",
    "realvalidito_api_key": "OPENDIALER_PROFILE_REALVALIDITO_KEY",
    "realvalidito_api_secret": "OPENDIALER_PROFILE_REALVALIDITO_SECRET",
}


def provision(args, client):
    url = f"https://{args.relay_ref}.supabase.co"
    admin = os.environ.get("OPENDIALER_RELAY_ADMIN_KEY")
    if not admin:
        raise ValueError("Set OPENDIALER_RELAY_ADMIN_KEY for the target relay")
    headers = {"apikey": admin}
    # The legacy service_role JWT goes in both headers. A Supabase secret key is a gateway API key, not a JWT.
    if not admin.startswith("sb_secret_"):
        headers["Authorization"] = f"Bearer {admin}"
    workspace, token = str(uuid4()), secrets.token_urlsafe(36)
    invitation = {
        "workspace_id": workspace,
        "label": args.label,
        "hash": hashlib.sha256(token.encode()).hexdigest(),
        "routes": [],
    }
    if args.pool is not None:
        routes = client.get(
            url + "/rest/v1/relay_routes",
            headers=headers,
            params={"select": "public_key", "connection_id": f"eq.{args.connection}"},
        )
        routes.raise_for_status()
        if len(routes.json()) != 1:
            raise ValueError("The selected application is not registered on this relay")
        signing_key = routes.json()[0]["public_key"]
        invitation.update(shared_pool_id=str(args.pool), shared_segment_id=args.segment)
    else:
        signing_key = args.signing_key
        invitation["routes"] = [
            {
                "connection_id": args.connection,
                "account_id": args.bootstrap_account,
                "public_key": signing_key,
            }
        ]
    invitation["settings"] = {
        "telnyx_connection_id": args.connection,
        "telephony_credential_id": args.credential,
        "sip_username": args.sip_user,
        "telnyx_public_key": signing_key,
    }
    client.post(url + "/rest/v1/relay_invitations", headers=headers, json=invitation).raise_for_status()
    for _ in range(3):
        try:
            response = client.post(url + "/functions/v1/managed-setup", json={"token": token})
        except httpx.RequestError:
            continue
        if response.status_code < 500:
            break
    else:
        raise ValueError(
            f"Setup function unavailable. Check workspace {workspace} before issuing another invite."
        )
    response.raise_for_status()
    profile = response.json()
    if (
        profile.get("workspace_id") != workspace
        or profile.get("url") != url
        or profile.get("password") != token
        or not str(profile.get("publishable_key", "")).startswith("sb_publishable_")
    ):
        raise ValueError("Setup response does not match the expected workspace and relay")
    return profile | {field: os.environ[name] for field, name in PROFILE_KEYS.items() if os.environ.get(name)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--relay-ref", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--connection", required=True, help="Shared Telnyx Call Control application ID")
    parser.add_argument("--credential", required=True, help="This rep's Telephony Credential ID")
    parser.add_argument("--sip-user", required=True, help="This rep's generated SIP username")
    parser.add_argument(
        "--output", type=Path, required=True, help="A new private JSON file outside the repository"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pool", type=UUID, help="Existing shared pool")
    mode.add_argument("--bootstrap-account", help="Register the application with its first workspace")
    parser.add_argument("--segment", help="This rep's HubSpot segment ID; required with --pool")
    parser.add_argument("--signing-key", help="Telnyx Ed25519 public key; required with --bootstrap-account")
    args = parser.parse_args()
    if not (
        re.fullmatch(r"[a-z]{20}", args.relay_ref)
        and 1 <= len(args.label) <= 100
        and re.fullmatch(r"[A-Za-z0-9_.+-]{1,128}", args.sip_user)
        and re.fullmatch(r"[A-Za-z0-9-]{1,128}", args.credential)
        and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", args.connection)
    ):
        parser.error("Invalid relay reference, label or Telnyx identifiers")
    if args.pool and (not re.fullmatch(r"[0-9]{1,40}", args.segment or "") or args.signing_key):
        parser.error("Specify the rep's segment with --pool; the relay supplies the signing key")
    if args.bootstrap_account and (not args.signing_key or args.segment):
        parser.error("Specify the Telnyx signing public key with --bootstrap-account")
    path = args.output.expanduser().resolve()
    if path.is_relative_to(ROOT) or not path.parent.is_dir() or path.exists():
        parser.error("Choose a new file in an existing private directory outside the repository")
    try:
        with httpx.Client(timeout=15, trust_env=False) as client:
            profile = provision(args, client)
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
            json.dump(profile, output, indent=2)
            output.write("\n")
    except httpx.HTTPStatusError as error:
        parser.exit(
            1, f"Relay rejected setup (HTTP {error.response.status_code}); verify target and invitation.\n"
        )
    except (ValueError, OSError, httpx.RequestError) as error:
        parser.exit(
            1,
            f"Profile setup stopped: {type(error).__name__}. {error if isinstance(error, ValueError) else ''}\n",
        )
    print(f"Private setup file saved to {path}")


if __name__ == "__main__":
    main()
