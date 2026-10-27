"""Pinned native runtime against a local fake Responses endpoint; no account or paid request."""

import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.mark.skipif(not os.getenv("OPENDIALER_TEST_CODEX_NATIVE"), reason="Opt-in native runtime contract")
@pytest.mark.asyncio
@pytest.mark.parametrize("code_mode", [False, True])
async def test_native_runtime_accepts_submission_and_preserves_usage(tmp_path, monkeypatch, code_mode):
    from app.providers import codex

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.path == "/v1/responses"
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            item = {
                "type": "function_call",
                "name": "submit_test",
                "call_id": "test-call",
                "arguments": '{"ok":true}',
            }
            if code_mode:
                item = {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "namespace": "functions",
                    "call_id": "test-call",
                    "input": "text(await tools.submit_test({ok:true}));",
                }
            events = [{"type": "response.created", "response": {"id": "fixture-response"}}]
            if len(requests) == 1:
                events += [
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {**item, "input" if code_mode else "arguments": ""},
                    },
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                ]
            events.append(
                {
                    "type": "response.completed",
                    "response": {
                        "id": "fixture-response",
                        "status": "completed",
                        "output": [item] if len(requests) == 1 else [],
                        "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
                    },
                }
            )
            for event in events:
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # The shipped binary must work without npm's other sibling resources or Node.
    original = codex.runtime_command()[0]
    binary = tmp_path / ("opendialer-codex.exe" if os.name == "nt" else "opendialer-codex")
    shutil.copy2(original, binary)
    from pathlib import Path

    helper = Path(original).with_name(
        "codex-code-mode-host.exe" if os.name == "nt" else "codex-code-mode-host"
    )
    shutil.copy2(helper, tmp_path / helper.name)
    if sys.platform == "darwin":
        root = Path(__file__).resolve().parents[3]
        for path, entitlement in (
            (binary, "BackendEntitlements.plist"),
            (tmp_path / helper.name, "CodexHostEntitlements.plist"),
        ):
            subprocess.run(  # noqa: S603 - disposable copies and repository entitlements
                [
                    "/usr/bin/codesign",
                    "--force",
                    "--sign",
                    "-",
                    "--options",
                    "runtime",
                    "--entitlements",
                    str(root / "apps/desktop/src-tauri" / entitlement),
                    str(path),
                ],
                check=True,
            )
    command = [str(binary), "app-server"] + [
        "-c",
        'model_provider="fixture"',
        "-c",
        'model_providers.fixture.name="Fixture"',
        "-c",
        f'model_providers.fixture.base_url="http://127.0.0.1:{server.server_port}/v1"',
        "-c",
        'model_providers.fixture.wire_api="responses"',
        "-c",
        "model_providers.fixture.requires_openai_auth=false",
    ]
    monkeypatch.setattr(codex, "runtime_command", lambda: command)
    try:
        result = await codex.CodexClient(tmp_path, "gpt-6.1-sol", "submit_test").complete(
            "Use submit_test once.",
            {"fixture": True},
            {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            },
        )
        assert result.data == {"ok": True}
        assert result.tool_call_id and result.request_id
        if not code_mode:
            assert result.tool_call_id == "test-call"
        assert result.usage == {"input_tokens": 20, "output_tokens": 8}
        assert len(requests) == 2
        assert all(request["store"] is False for request in requests)
        tools = json.dumps(
            requests[0].get("tools", [])
            + [item for item in requests[0]["input"] if item["type"] == "additional_tools"]
        )
        assert "submit_test" in tools
        # The generic code-mode documentation mentions exec_command as an example;
        # check the actual tool declarations rather than that explanatory prose.
        assert '"name": "exec_command"' not in tools and "### `exec_command`" not in tools
        assert '"name": "web_search"' not in tools
        assert not (tmp_path / "auth.json").exists()
        assert not (tmp_path / "sessions").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
