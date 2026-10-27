"""Codex login stays personal; managed credentials survive refresh and failed imports."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_api_and_worker_serialize_the_same_personal_login(tmp_path):
    from app.providers.codex import personal_lock

    script = """
import asyncio, sys
from pathlib import Path
from app.providers.codex import personal_lock
async def main():
    try:
        async with asyncio.timeout(0.05):
            async with personal_lock(Path(sys.argv[1])):
                print('acquired too early', flush=True)
                return
    except TimeoutError:
        print('blocked', flush=True)
    async with personal_lock(Path(sys.argv[1])):
        Path(sys.argv[1], 'acquired').write_text('worker')
asyncio.run(main())
"""
    # A separate process models the shared-server worker, not another task's local lock.
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    async with personal_lock(tmp_path):
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", script, str(tmp_path), env=env, stdout=asyncio.subprocess.PIPE
        )
        assert await asyncio.wait_for(process.stdout.readline(), 15) == b"blocked\n"
        assert not (tmp_path / "acquired").exists()
    try:
        assert await asyncio.wait_for(process.wait(), 15) == 0
        assert (tmp_path / "acquired").read_text() == "worker"
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.fixture
def runtime_fixture(tmp_path, monkeypatch):
    from app.providers import codex

    script = tmp_path / "runtime.py"
    script.write_text("""
import json, os, sys
from pathlib import Path
home = Path(os.environ["CODEX_HOME"])
assert os.environ["HOME"] == str(home)
assert "OPENAI_API_KEY" not in os.environ
def send(value):
 print(json.dumps(value), flush=True)
for line in sys.stdin:
 m = json.loads(line)
 method = m.get("method")
 result = {}
 if method == "initialized": continue
 if method == "account/read":
  data = json.loads((home / "auth.json").read_text())
  if m["params"].get("refreshToken"):
   data["tokens"]["refresh_token"] = "rotated-by-child"
   (home / "auth.json").write_text(json.dumps(data))
  result = {"account":{"type":"chatgpt","email":"child@example.test","planType":"pro"}}
 if method == "thread/start":
  assert m["params"]["ephemeral"] and m["params"]["environments"] == []
  assert m["params"]["sandbox"] == "read-only"
  tool = m["params"]["dynamicTools"][0]["name"]
  result = {"thread":{"id":"thread-test"}}
 if method == "turn/start":
  data = json.loads((home / "auth.json").read_text())
  data["tokens"]["refresh_token"] = "rotated-during-inference"
  (home / "auth.json").write_text(json.dumps(data))
  mode = json.loads(m["params"]["input"][0]["text"])["mode"]
  send({"id":m["id"],"result":{"turn":{"id":"turn-test"}}})
  if mode == "prose":
   send({"method":"turn/completed","params":{"turn":{"id":"turn-test","status":"completed"}}})
  else:
   send({"id":99,"method":"item/tool/call", "params":{"tool": tool if mode == "valid" else "exec_command", "arguments":{"opener":"Hello"},"callId":"tool-test","threadId":"thread-test","turnId":"turn-test"}})
  continue
 if "method" not in m:
  send({"method":"thread/tokenUsage/updated","params":{"turnId":"turn-test","tokenUsage":{"total":{"inputTokens":12,"outputTokens":4}}}})
  send({"method":"turn/completed","params":{"turn":{"id":"turn-test","status":"completed"}}})
  continue
 send({"id":m["id"],"result":result})
""")
    monkeypatch.setattr(codex, "runtime_command", lambda: [sys.executable, str(script)])
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-child")
    return codex


@pytest.mark.asyncio
async def test_subprocess_refresh_persists_and_tool_only_protocol(tmp_path, runtime_fixture):
    codex = runtime_fixture
    home = tmp_path / "personal"
    codex.save_login(home, login())
    assert (await codex.CodexRuntime(home).account(refresh=True))["email"] == "child@example.test"
    assert json.loads((home / "auth.json").read_text())["tokens"]["refresh_token"] == "rotated-by-child"
    client = codex.CodexClient(home, "fixture", "submit_script_write")
    result = await client.complete("fixture", {"mode": "valid"}, {"type": "object"})
    assert result.data == {"opener": "Hello"}
    assert result.tool_call_id == "tool-test" and result.request_id == "turn-test"
    assert result.usage == {"input_tokens": 12, "output_tokens": 4}
    for mode in ("prose", "wrong_tool"):
        with pytest.raises(codex.InferenceError):
            await client.complete("fixture", {"mode": mode}, {"type": "object"})
    assert (
        json.loads((home / "auth.json").read_text())["tokens"]["refresh_token"] == "rotated-during-inference"
    )
    assert {p.name for p in home.iterdir()} == {"auth.json", ".lock"}


def test_failed_import_preserves_login_and_personal_route(workspace, app, monkeypatch):
    from app.providers.codex import CodexClient, CodexRuntime, InferenceError, profile_home, save_login

    service = app.state.service
    home = profile_home(service.settings.data_dir, workspace["user_id"])
    save_login(home, login())

    async def failed(self, refresh=False):
        raise InferenceError("Login revoked; import again")

    monkeypatch.setattr(CodexRuntime, "account", failed)
    result = workspace["client"].post(
        "/v1/inference/codex/import", headers=workspace["headers"], json=login()
    )
    assert result.status_code == 400
    assert json.loads((home / "auth.json").read_text()) == login()
    service.configs.write(workspace["tenant_id"], {"inference_mode": "codex"})
    assert isinstance(
        service.inference(workspace["tenant_id"], "script_write", workspace["user_id"]), CodexClient
    )
    assert service.inference(workspace["tenant_id"], "script_write", "another-user") is None
    assert service.inference(workspace["tenant_id"], "script_write") is None
    assert workspace["client"].get("/v1/inference/codex").status_code in (401, 403)


def login():
    return {
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": "fixture-access",
            "refresh_token": "fixture-refresh",
            "id_token": "fixture-identity",
            "account_id": "fixture-account",
        },
    }


def test_personal_import_refresh_disconnect(workspace, app, monkeypatch):
    from app.providers.codex import CodexRuntime, profile_home

    async def account(self, refresh=False):
        assert refresh
        path = self.home / "auth.json"
        data = json.loads(path.read_text())
        data["tokens"]["refresh_token"] = "rotated-fixture-refresh"
        path.write_text(json.dumps(data))
        return {"type": "chatgpt", "email": "fixture@example.test", "planType": "pro"}

    monkeypatch.setattr(CodexRuntime, "account", account)
    client, headers = workspace["client"], workspace["headers"]
    url = "/v1/inference/codex"
    assert client.get(url, headers=headers).json()["connected"] is False
    result = client.post(url + "/import", headers=headers, json=login())
    assert result.status_code == 200, result.text
    assert result.json()["connected"] is True
    assert "token" not in result.text
    home = profile_home(app.state.service.settings.data_dir, workspace["user_id"])
    saved = json.loads((home / "auth.json").read_text())
    assert saved["tokens"]["refresh_token"] == "rotated-fixture-refresh"
    assert client.post(url + "/import", headers=headers, json={"OPENAI_API_KEY": "fake"}).status_code == 422
    malformed = client.post(url + "/import", headers=headers, json=[login()])
    assert malformed.status_code == 422
    assert "fixture-access" not in malformed.text
    assert json.loads((home / "auth.json").read_text()) == saved
    assert client.get(url, headers=headers).json()["connected"] is True
    assert client.delete(url, headers=headers).status_code == 200
    assert not (home / "auth.json").exists()
    assert client.get(url, headers=headers).json()["connected"] is False


@pytest.mark.asyncio
async def test_codex_requires_named_tool_and_preserves_receipt(tmp_path, monkeypatch):
    from app.providers.codex import CodexClient, CodexRuntime
    from app.providers.inference import InferenceError

    async def complete(self, model, instructions, context, schema, tool_name):
        assert tool_name == "submit_script_write"
        return {
            "arguments": {"opener": "Hello"},
            "call_id": "call-fixture",
            "request_id": "turn-fixture",
            "usage": {"input_tokens": 12},
        }

    monkeypatch.setattr(CodexRuntime, "complete", complete)
    client = CodexClient(tmp_path, "fixture-model", "submit_script_write")
    result = await client.complete("instructions", {}, {"type": "object"})
    assert result.data == {"opener": "Hello"}
    assert result.tool_call_id == "call-fixture"
    assert result.request_id == "turn-fixture"
    assert result.usage["input_tokens"] == 12
    assert client.billing == "chatgpt_plan"

    async def prose(*args):
        return {"text": '{"opener":"Do not execute prose"}'}

    monkeypatch.setattr(CodexRuntime, "complete", prose)
    with pytest.raises(InferenceError):
        await client.complete("instructions", {}, {})
