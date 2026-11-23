"""Write finished scripts for a lead CSV with the app's own preparation, then export them (docs/ONE_GENERATOR_PLAN.md).

    python -m app.cli generate-scripts LEADS.csv --master MASTER.md [--out OUT.csv] [--workers 4]

It runs a private workspace in a work folder beside the output, never the installed one. Leads go through the same
import, research, checks, review and rendering as in the app. Interrupt it and run the same command to continue.
"""

import argparse
import csv
import hashlib
import json
import logging
import os
import secrets
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import warnings
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

LIST_NAME = "Generated scripts"
IMPORT_CHUNK = 500
OPEN = {"missing", "queued", "stale", "preparing", "working"}


def installed_workspace():
    return Path.home() / "Library/Application Support/com.opendialer.desktop/local-3/opendialer.db"


def installed_master(database):
    """The master saved in the installed app, read-only, when --master is not given."""
    if not database.is_file():
        return None
    query = "select json_extract(data, '$.script.template.markdown') from campaigns order by created_at, id limit 1"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
        found = conn.execute(query).fetchone()
    return found[0] if found else None


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream, strict=True)
        headers = next(reader, [])
        normalized = ["".join(c for c in head.casefold() if c.isalnum()) for head in headers]
        if not headers or not all(normalized) or len(set(normalized)) != len(headers):
            raise ValueError("Give each CSV column a unique, nonempty header.")
        rows = []
        for cells in reader:
            if any(field.strip() for field in cells[len(headers) :]):
                raise ValueError(
                    f"CSV row {reader.line_num} has more values than headers. Quote fields with commas."
                )
            if any(field.strip() for field in cells):
                rows.append(dict(zip(headers, (cells + [""] * len(headers))[: len(headers)], strict=True)))
    if not rows:
        raise ValueError("The CSV has no lead rows.")
    return headers, rows


def write_csv(target, headers, rows):
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8-sig", newline="", dir=target.parent, delete=False, suffix=".partial"
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(stream.name, target)


def digest(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()[:16]


def parser():
    parser = argparse.ArgumentParser(prog="generate-scripts", description=__doc__.splitlines()[0])
    parser.add_argument("csv", type=Path, help="CSV of leads")
    parser.add_argument(
        "--master", type=Path, help="Markdown master; default is the master saved in OpenDialer"
    )
    parser.add_argument(
        "--out", "--output", dest="out", type=Path, help="Output CSV, default INPUT-scripts.csv"
    )
    parser.add_argument("--work-dir", type=Path, help="Private workspace, default OUTPUT.work")
    parser.add_argument(
        "--workers", type=int, default=4, choices=range(1, 5), metavar="1-4", help="AI threads at once"
    )
    parser.add_argument(
        "--leads-per-thread",
        type=int,
        default=5,
        choices=range(1, 21),
        metavar="1-20",
        help="Leads per AI thread",
    )
    parser.add_argument(
        "--codex-home", type=Path, help="Signed-in Codex folder, default $CODEX_HOME or ~/.codex"
    )
    parser.add_argument(
        "--retry-wait", type=int, default=40, help="Minutes to wait for due retries (default 40)"
    )
    parser.add_argument(
        "--restart", action="store_true", help="Discard the work folder from a different input"
    )
    parser.add_argument("--simulation", action="store_true", help=argparse.SUPPRESS)
    return parser


class Api:
    def __init__(self, client, local_token):
        self.client, self.local, self.headers = client, local_token, {}

    def call(self, method, path, body=None, **params):
        response = self.client.request(method, path, json=body, params=params or None, headers=self.headers)
        if response.status_code >= 400:
            detail = response.json().get("detail") if response.content else response.status_code
            raise ValueError(f"{method} {path} failed: {detail}")
        return response.json() if response.content else None

    def sign_in(self, seller):
        local = {"X-Local-Token": self.local}
        response = self.client.post("/v1/local/session", headers=local)
        if response.status_code == 409:
            response = self.client.post(
                "/v1/setup", headers=local, json={"seller_name": seller, "name": "Script author"}
            )
        response.raise_for_status()
        self.headers = {"Authorization": f"Bearer {response.json()['access_token']}"}


def key_file(work):
    """A key of the work folder's own, so its copy of the ChatGPT login is not readable with the app's key."""
    path = work / "secret.key"
    if not path.exists():
        from cryptography.fernet import Fernet

        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(Fernet.generate_key().decode())
    return path


def import_login(api, codex_home):
    path = codex_home / "auth.json"
    if not path.is_file():
        raise ValueError(f"No Codex login at {path}. Sign in with the Codex CLI, or pass --codex-home.")
    original = json.loads(path.read_text())
    api.call("POST", "/v1/inference/codex/import", original)
    return original


def write_back_login(work, user_id, codex_home, original):
    """Codex may rotate the login during a long run; give the newer tokens back so the Codex CLI stays signed in."""
    from app.platform.secret_files import read_private
    from app.providers.codex import profile_home

    copy = profile_home(work, user_id) / "auth.json"
    if not copy.exists():
        return
    current = read_private(copy)
    if current.get("tokens") == original.get("tokens"):
        return
    path = codex_home / "auth.json"
    disk = json.loads(path.read_text())
    if disk.get("tokens") != original.get("tokens"):
        print("Codex signed in again during the run; its login was left as it is.", file=sys.stderr)
        return
    merged = {**disk, "tokens": {**disk["tokens"], **current["tokens"]}}
    if current.get("last_refresh"):
        merged["last_refresh"] = current["last_refresh"]
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, suffix=".partial") as stream:
        json.dump(merged, stream, indent=2)
    os.chmod(stream.name, 0o600)
    os.replace(stream.name, path)


def phone_of(row, column_map):
    from app.compliance.phone import normalize_phone
    from app.leads import intake

    values = {**row, **intake.csv_identity(row, column_map)}
    return normalize_phone(str(values.get("phone", ""))).e164


def watch_progress(database, total, stop):
    """One line every 30 seconds while the app works, read-only from the work folder's database."""
    query = "select json_extract(data, '$.preparation.status'), count(*) from leads group by 1"
    last = None
    while not stop.wait(30):
        try:
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
                counts = dict(conn.execute(query).fetchall())
        except sqlite3.Error:
            continue
        line = (
            f"  working: {counts.get('ready', 0)} of {total} ready, {counts.get('failed', 0)} failed so far"
        )
        if line != last:
            print(line, flush=True)
            last = line


def wait_for_scripts(api, retry_wait, total):
    """Run the app's queued AI work until every lead is ready, skipped, out of retries or past the retry window."""
    give_up = time.monotonic() + retry_wait * 60
    last = None
    while True:
        receipt = api.call("POST", "/v1/admin/jobs/run")
        exported = api.call("GET", "/v1/preparation/export", list_name=LIST_NAME)
        waiting = [item for item in exported["not_ready"] if item["status"] in OPEN]
        failed = [item for item in exported["not_ready"] if item["status"] == "failed"]
        line = f"{exported['ready']} of {total} scripts ready, {len(failed)} failed for now"
        if line != last:
            print(line, flush=True)
            last = line
        if waiting:
            if not receipt.get("processed"):
                # Nothing could run (held by an interrupted run's lease, or no thread queued): queue, then wait.
                time.sleep(10)
                api.call("PUT", "/v1/admin/preparation", api.call("GET", "/v1/preparation")["settings"])
            continue
        due = sorted(item["next_attempt_at"] for item in failed if item.get("next_attempt_at"))
        if not due:
            return exported
        seconds = (datetime.fromisoformat(due[0]) - datetime.now(UTC)).total_seconds()
        if time.monotonic() + seconds > give_up:
            return exported
        if seconds > 0:
            print(f"Waiting {int(seconds // 60) + 1} min for {len(due)} retries.", flush=True)
            time.sleep(seconds + 1)
        # Due retries are queued by the app's preparation tick, which saving the same policy runs.
        policy = api.call("GET", "/v1/preparation")["settings"]
        api.call("PUT", "/v1/admin/preparation", policy)


def main(argv=None):
    args = parser().parse_args(argv)
    from app.preparation.csv_scripts import split_script
    from app.preparation.portable import COLUMNS
    from app.preparation.templates import ScriptError, parse_markdown, template_version

    source = args.csv.expanduser().resolve()
    out = (args.out or source.with_name(source.stem + "-scripts.csv")).expanduser().resolve()
    work = (args.work_dir or out.with_name(out.name + ".work")).expanduser().resolve()
    codex_home = (
        args.codex_home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    ).expanduser()
    headers, rows = read_csv(source)
    master = args.master.expanduser().read_text(encoding="utf-8-sig") if args.master else None
    master = master or installed_master(installed_workspace())
    if not master:
        raise ValueError("Pass --master with your Markdown script, or save one in OpenDialer.")
    template = parse_markdown(master)
    manifest_path = work / "manifest.json"
    key = {"input": digest(source.read_bytes()), "master": template_version(template), "output": str(out)}
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    if manifest and {k: manifest.get(k) for k in key} != key:
        changed = [name for name in key if manifest.get(name) != key[name]]
        if not args.restart:
            raise ValueError(
                f"{work} holds a run of a different {' and '.join(changed)}. "
                "Run the original command, or add --restart to start over."
            )
        shutil.rmtree(work)
        manifest = None
    if out.exists() and not manifest:
        raise ValueError(f"Output already exists: {out}. Choose another path with --out.")
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest = manifest or {**key, "imported": False}
    manifest_path.write_text(json.dumps(manifest))

    finished, waiting = {}, []
    for index, row in enumerate(rows):
        try:
            _, script = split_script(row)
        except ScriptError as error:
            raise ValueError(f"CSV row {index + 2}: {error}") from None
        (finished.__setitem__(index, row) if script else waiting.append(index))

    os.environ.pop("OPENDIALER_SECRET_KEY", None)
    os.environ["OPENDIALER_SECRET_KEY_FILE"] = str(key_file(work))
    warnings.filterwarnings("ignore", message=".*httpx.*")
    # App warnings (unreadable pages and the like) go to the work folder's log, not the terminal.
    logging.basicConfig(
        filename=work / "run.log", level=logging.INFO, format="%(asctime)s %(name)s %(message)s"
    )
    from fastapi.testclient import TestClient

    from app.leads import intake
    from app.main import create_app
    from app.platform.config import Settings

    token = secrets.token_urlsafe(24)
    settings = Settings(
        data_dir=work,
        database_url=f"sqlite:///{work / 'opendialer.db'}",
        local_token=token,
        simulation=args.simulation,
        worker_enabled=False,
    )
    app = create_app(settings)
    original_login, user_id = None, None
    try:
        with TestClient(app) as client:
            api = Api(client, token)
            api.sign_in("Your business")
            me = api.call("GET", "/v1/workspace")
            user_id = me["user"]["id"]
            base = api.call("GET", "/v1/optimization/script")["base"]
            if (base.get("template") or {}).get("markdown") != master:
                api.call(
                    "POST",
                    "/v1/admin/script/import",
                    {"markdown": master, "expected_version": base["version"], "preview": False},
                )
            if not args.simulation:
                original_login = import_login(api, codex_home)
            policy = {"leads_per_agent": args.leads_per_thread, "agents_at_once": args.workers}
            api.call("PUT", "/v1/admin/preparation", policy)
            if waiting and not manifest["imported"]:
                for start in range(0, len(waiting), IMPORT_CHUNK):
                    batch = [rows[index] for index in waiting[start : start + IMPORT_CHUNK]]
                    api.call(
                        "POST",
                        "/v1/leads/import",
                        {"campaign_id": me["campaign"]["id"], "list_name": LIST_NAME, "rows": batch},
                    )
                manifest["imported"] = True
                manifest_path.write_text(json.dumps(manifest))
            exported = {"rows": [], "not_ready": []}
            if waiting:
                api.call("POST", "/v1/preparation/lists", {"list_name": LIST_NAME})
                stop = threading.Event()
                watcher = threading.Thread(
                    target=watch_progress, args=(work / "opendialer.db", len(waiting), stop), daemon=True
                )
                watcher.start()
                try:
                    exported = wait_for_scripts(api, args.retry_wait, len(waiting))
                finally:
                    stop.set()
    finally:
        if original_login and user_id:
            write_back_login(work, user_id, codex_home, original_login)
        app.state.db.engine.dispose()

    column_map = intake.guess_map(headers, csv_identity=True)
    by_phone = {row["phone"]: row for row in exported["rows"]}
    reasons = {item["phone"]: item["reason"] for item in exported["not_ready"]}
    columns = headers + [name for name in COLUMNS if name not in headers]
    output, problems = [], []
    for index, row in enumerate(rows):
        if index in finished:
            output.append(row)
            continue
        phone = phone_of(row, column_map)
        script = by_phone.get(phone)
        output.append({**row, **{name: script[name] for name in COLUMNS}} if script else row)
        if not script:
            problems.append(
                (index + 2, row, reasons.get(phone) or ("no valid phone" if not phone else "not imported"))
            )
    write_csv(out, columns, output)
    ready = len(rows) - len(problems)
    print(f"{ready} of {len(rows)} scripts ready in {out}")
    trimmed = [row for row in exported["rows"] if row.get("dropped_by_review")]
    if trimmed:
        # The app publishes a script without the AI lines its review rejected; say so rather than ship it silently.
        print(f"{len(trimmed)} scripts lost AI-written lines in the review:", file=sys.stderr)
        for row in trimmed[:20]:
            lines = "; ".join(row.get("dropped_text", {}).values()) or ", ".join(row["dropped_by_review"])
            print(f"  {row['name']}: {lines}", file=sys.stderr)
    for line, row, reason in problems:
        name = row.get("name") or row.get("Name") or ""
        print(f"  Row {line} {name}: {reason}", file=sys.stderr)
    if problems:
        print("Run the same command again to retry the rest; finished scripts are kept.", file=sys.stderr)
    return int(bool(problems))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, sqlite3.Error, csv.Error) as error:
        print(f"Script generation stopped: {error}", file=sys.stderr)
        raise SystemExit(1) from None
