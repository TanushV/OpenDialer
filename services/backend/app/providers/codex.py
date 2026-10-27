"""Personal Codex managed login and tool-only app-server inference.

Only auth.json persists. Each invocation has a disposable runtime home/workspace,
and a cross-process personal lock prevents API/worker refresh-token races.
"""

import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from app.providers.inference import InferenceError, InferenceResult

MAX_LOGIN_BYTES = 65536
RECONNECT = "Codex could not complete the request. Check your connection and plan limits; if the login expired or was revoked, sign in with Codex again and import a fresh auth.json."


def profile_home(directory, user_id):
    return Path(directory).resolve() / "codex" / hashlib.sha256(user_id.encode()).hexdigest()


def runtime_command():
    suffix = ".exe" if os.name == "nt" else ""
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable)
        suffix = executable.name.removeprefix("opendialer-backend")
        path = executable.with_name("opendialer-codex" + suffix)
    else:
        path = Path(shutil.which("opendialer-codex") or "")
        if not path.is_file():
            root = Path(__file__).resolve().parents[4]
            matches = list(
                (root / "apps/desktop/node_modules/@openai").glob("codex-*/vendor/*/bin/codex" + suffix)
            )
            path = matches[0] if len(matches) == 1 else path
    if not path.is_file():
        raise InferenceError(
            "The Codex component is missing. Install a complete OpenDialer build; source checkouts need npm ci in apps/desktop."
        )
    return [str(path.resolve()), "app-server"]


def validate_login(data):
    if not isinstance(data, dict) or len(json.dumps(data).encode()) > MAX_LOGIN_BYTES:
        raise ValueError("Choose the complete Codex auth.json file (maximum 64 KB).")
    tokens = data.get("tokens")
    if (
        data.get("auth_mode", "chatgpt") != "chatgpt"
        or data.get("OPENAI_API_KEY")
        or not isinstance(tokens, dict)
        or any(
            not isinstance(tokens.get(k), str) or not tokens[k].strip()
            for k in ("access_token", "refresh_token", "id_token", "account_id")
        )
    ):
        raise ValueError(
            "Import a ChatGPT Codex login with its refresh token, not an API key or a pasted access token."
        )
    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {k: tokens[k] for k in ("access_token", "refresh_token", "id_token", "account_id")},
        **({"last_refresh": data["last_refresh"]} if isinstance(data.get("last_refresh"), str) else {}),
    }


def save_login(home, data):
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = home / "auth.json"
    temporary = home / "auth.tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(data, stream)
    temporary.replace(path)


@asynccontextmanager
async def personal_lock(home):
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(home / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        async with asyncio.timeout(120):
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    await asyncio.sleep(0.1)
        yield
    finally:
        os.close(descriptor)


class CodexRuntime:
    def __init__(self, home):
        self.home = Path(home)
        self.sequence = 0
        self.events = []

    @asynccontextmanager
    async def session(self):
        command = runtime_command()
        with tempfile.TemporaryDirectory(prefix="opendialer-codex-") as temporary:
            run_home = Path(temporary)
            workspace = run_home / "workspace"
            workspace.mkdir()
            auth = self.home / "auth.json"
            if auth.exists():
                save_login(run_home, json.loads(auth.read_text()))
            # No user config, plugins, credentials or workspace instructions are inherited.
            config = "\n".join(
                [
                    'cli_auth_credentials_store = "file"',
                    'web_search = "disabled"',
                    'history.persistence = "none"',
                    "analytics.enabled = false",
                    "features.shell_tool = false",
                    "features.unified_exec = false",
                    "features.apps = false",
                    "features.multi_agent = false",
                    "features.multi_agent_v2 = false",
                    "features.code_mode = false",
                    "features.goals = false",
                    "features.sleep_tool = false",
                    "tools.update_plan.enabled = false",
                    "features.browser_use = false",
                    "features.computer_use = false",
                    "features.image_generation = false",
                    "features.hooks = false",
                    "features.memories = false",
                    "features.shell_snapshot = false",
                    "features.view_image = false",
                ]
            )
            (run_home / "config.toml").write_text(config)
            allowed = {
                "PATH",
                "SystemRoot",
                "WINDIR",
                "TEMP",
                "TMP",
                "LANG",
                "LC_ALL",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
            }
            env = {k: v for k, v in os.environ.items() if k in allowed}
            env.update(CODEX_HOME=str(run_home), HOME=str(run_home), USERPROFILE=str(run_home))
            process = None
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=workspace,
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    limit=2 * 1024 * 1024,
                )
                self.process, self.workspace = process, workspace
                async with asyncio.timeout(120):
                    await self.request(
                        "initialize",
                        {
                            "clientInfo": {"name": "opendialer", "version": "0.5.1"},
                            "capabilities": {"experimentalApi": True},
                        },
                    )
                    await self.send({"method": "initialized", "params": {}})
                    yield
            except (OSError, ValueError, TimeoutError, EOFError) as exc:
                raise InferenceError(RECONNECT) from exc
            finally:
                if process is not None and process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 5)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
                # Managed auth may rotate a refresh token even when the later inference fails.
                if (run_home / "auth.json").exists():
                    save_login(self.home, json.loads((run_home / "auth.json").read_text()))

    async def send(self, value):
        self.process.stdin.write((json.dumps(value) + "\n").encode())
        await self.process.stdin.drain()

    async def receive(self):
        line = await self.process.stdout.readline()
        if not line:
            raise EOFError
        return json.loads(line)

    async def request(self, method, params):
        self.sequence += 1
        identity = self.sequence
        await self.send({"id": identity, "method": method, "params": params})
        while True:
            message = await self.receive()
            if message.get("id") == identity and "method" not in message:
                if "error" in message:
                    raise InferenceError(RECONNECT)
                return message["result"]
            self.events.append(message)

    async def account(self, refresh=False):
        async with self.session():
            result = await self.request("account/read", {"refreshToken": refresh})
            account = result.get("account")
            if not account or account.get("type") != "chatgpt":
                raise InferenceError(RECONNECT)
            return {k: account.get(k) for k in ("type", "email", "planType")}

    async def complete(self, model, instructions, context, schema, tool_name):
        async with self.session():
            thread = await self.request(
                "thread/start",
                {
                    "model": model,
                    "ephemeral": True,
                    "cwd": str(self.workspace),
                    "environments": [],
                    "selectedCapabilityRoots": [],
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "baseInstructions": instructions
                    + "\nUse only the supplied context. Call "
                    + tool_name
                    + " exactly once with the result, then stop. Do not use other tools.",
                    "dynamicTools": [
                        {
                            "type": "function",
                            "name": tool_name,
                            "description": "Submit the result for OpenDialer validation. This does not directly change application state.",
                            "inputSchema": schema,
                        }
                    ],
                },
            )
            thread_id = thread["thread"]["id"]
            turn = await self.request(
                "turn/start",
                {"threadId": thread_id, "input": [{"type": "text", "text": json.dumps(context)}]},
            )
            turn_id = turn["turn"]["id"]
            result, usage = None, {}
            while True:
                message = self.events.pop(0) if self.events else await self.receive()
                method, params = message.get("method"), message.get("params", {})
                if "id" in message:
                    if (
                        method != "item/tool/call"
                        or params.get("tool") != tool_name
                        or result is not None
                        or params.get("threadId") != thread_id
                        or params.get("turnId") != turn_id
                        or not isinstance(params.get("arguments"), dict)
                        or not params.get("callId")
                    ):
                        await self.send(
                            {"id": message["id"], "error": {"code": -32601, "message": "Tool not allowed"}}
                        )
                        raise InferenceError("Codex requested an unsupported action; no result was applied.")
                    result = {
                        "arguments": params["arguments"],
                        "call_id": params["callId"],
                        "request_id": turn_id,
                    }
                    await self.send(
                        {
                            "id": message["id"],
                            "result": {
                                "contentItems": [
                                    {"type": "inputText", "text": "Received for validation. Stop now."}
                                ],
                                "success": True,
                            },
                        }
                    )
                elif method == "thread/tokenUsage/updated" and params.get("turnId") == turn_id:
                    total = params["tokenUsage"]["total"]
                    usage = {"input_tokens": total["inputTokens"], "output_tokens": total["outputTokens"]}
                elif method == "turn/completed" and params.get("turn", {}).get("id") == turn_id:
                    if params["turn"].get("status") != "completed" or result is None:
                        raise InferenceError("Codex did not submit a completed tool result. " + RECONNECT)
                    return {**result, "usage": usage}


class CodexClient:
    billing = "chatgpt_plan"

    def __init__(self, home, model, tool_name):
        self.home, self.model, self.tool_name = home, model, tool_name

    async def complete(self, instructions, context, schema):
        async with personal_lock(self.home):
            result = await CodexRuntime(self.home).complete(
                self.model, instructions, context, schema, self.tool_name
            )
        if not isinstance(result.get("arguments"), dict) or not result.get("call_id"):
            raise InferenceError("Codex must submit a named tool result.")
        return InferenceResult(
            result["arguments"], result.get("usage", {}), result.get("request_id"), result["call_id"]
        )


def bind_api(app, service, principal):
    from fastapi import Body, Depends, HTTPException

    def home(p):
        return profile_home(service.settings.data_dir, p["user_id"])

    @app.get("/v1/inference/codex")
    def status(p=Depends(principal)):
        directory = home(p)
        connected = (directory / "auth.json").is_file()
        metadata = directory / "account.json"
        return {
            "connected": connected,
            "account": json.loads(metadata.read_text()) if connected and metadata.exists() else None,
        }

    @app.post("/v1/inference/codex/import")
    async def import_login(data: object = Body(), p=Depends(principal)):
        try:
            data = validate_login(data)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        directory = home(p)
        async with personal_lock(directory):
            with tempfile.TemporaryDirectory(prefix="import-", dir=directory) as temporary:
                candidate = Path(temporary)
                save_login(candidate, data)
                try:
                    account = await CodexRuntime(candidate).account(refresh=True)
                except InferenceError as exc:
                    raise HTTPException(400, str(exc)) from None
                save_login(directory, json.loads((candidate / "auth.json").read_text()))
                (directory / "account.json").write_text(json.dumps(account))
        return {"connected": True, "account": account}

    @app.delete("/v1/inference/codex")
    async def disconnect(p=Depends(principal)):
        async with personal_lock(home(p)):
            (home(p) / "auth.json").unlink(missing_ok=True)
            (home(p) / "account.json").unlink(missing_ok=True)
        return {"connected": False, "account": None}
