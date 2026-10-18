import json

import httpx
import pytest
from app.providers.batch import OpenAIBatch


@pytest.mark.asyncio
async def test_batch_submission_binds_original_intent_and_request_digest():
    seen = []
    metadata = {"opendialer_intent": "local-original", "request_digest": "a" * 64}

    def handler(request):
        seen.append(request)
        if request.url.path == "/v1/files":
            return httpx.Response(200, json={"id": "file-original"})
        body = json.loads(request.content)
        assert body["metadata"] == metadata
        assert body["input_file_id"] == "file-original"
        return httpx.Response(
            200, json={"id": "batch-original", "status": "validating", "metadata": metadata}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = OpenAIBatch("synthetic-key", http=http, metadata=metadata)
        result = await provider.submit([{"custom_id": "lead:unique", "body": {"store": False}}])
    assert result["id"] == "batch-original" and len(seen) == 2


@pytest.mark.asyncio
async def test_batch_result_read_verifies_identity_before_downloading_content():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200, json={"id": "batch-other", "status": "completed", "output_file_id": "file-other"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ValueError):
            await OpenAIBatch("synthetic-key", http=http).retrieve("batch-original")
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_legacy_batch_input_can_be_verified_by_original_custom_ids_and_context():
    expected = [{"custom_id": "lead:unique", "body": {"store": False}}]

    def handler(request):
        assert request.method == "GET" and request.url.path == "/v1/files/file-original/content"
        return httpx.Response(200, text="\n".join(json.dumps(r) for r in expected))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await OpenAIBatch("synthetic-key", http=http).input_requests("file-original") == expected


@pytest.mark.asyncio
async def test_batch_output_is_bounded_before_json_decoding():
    def handler(request):
        if request.url.path == "/v1/batches/batch-original":
            return httpx.Response(
                200, json={"id": "batch-original", "status": "completed", "output_file_id": "file-output"}
            )
        return httpx.Response(200, content=b" " * (8 * 1024 * 1024 + 1))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ValueError, match="size"):
            await OpenAIBatch("synthetic-key", http=http).retrieve("batch-original")
