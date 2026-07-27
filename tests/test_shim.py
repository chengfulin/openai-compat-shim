from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI
from openai import AsyncOpenAI
from pydantic import BaseModel

from app.config import Settings
from app.main import create_app
from tests.conftest import UPSTREAM_MODEL, UpstreamRecorder, build_upstream


async def test_alias_is_rewritten_upstream_and_echoed_back(
    client: httpx.AsyncClient, recorder: UpstreamRecorder
) -> None:
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "gemma4-26b", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert recorder.last_payload["model"] == UPSTREAM_MODEL
    # The caller keeps seeing the name it asked for.
    assert resp.json()["model"] == "gemma4-26b"


async def test_alias_lookup_is_case_insensitive(
    client: httpx.AsyncClient, recorder: UpstreamRecorder
) -> None:
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "GEMMA4-26B", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert recorder.last_payload["model"] == UPSTREAM_MODEL


async def test_unaliased_model_passes_through_untouched(
    client: httpx.AsyncClient, recorder: UpstreamRecorder
) -> None:
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": UPSTREAM_MODEL, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert recorder.last_payload["model"] == UPSTREAM_MODEL
    assert resp.json()["model"] == UPSTREAM_MODEL


async def test_missing_model_is_rejected(client: httpx.AsyncClient) -> None:
    resp = await client.post("/v1/chat/completions", json={"messages": []})
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "invalid_request_error"


async def test_authorization_header_is_forwarded(
    client: httpx.AsyncClient, recorder: UpstreamRecorder
) -> None:
    await client.post(
        "/v1/chat/completions",
        json={"model": "gemma4-26b", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert recorder.last_headers["authorization"] == "Bearer test-key"


async def test_upstream_api_key_fills_in_when_caller_sends_none(
    recorder: UpstreamRecorder,
) -> None:
    app = create_app(Settings(upstream_base_url="http://upstream", upstream_api_key="env-key"))
    app.state.upstream = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_upstream(recorder)), base_url="http://upstream"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://shim"
    ) as anon:
        resp = await anon.post(
            "/v1/chat/completions",
            json={"model": "gemma4-26b", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200
    assert recorder.last_headers["authorization"] == "Bearer env-key"


async def test_upstream_401_is_passed_through(shim: FastAPI) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=shim), base_url="http://shim"
    ) as anon:
        resp = await anon.post(
            "/v1/chat/completions",
            json={"model": "gemma4-26b", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 401
    assert resp.json() == {"error": "Unauthorized"}


async def test_unreachable_upstream_becomes_502(settings: Settings) -> None:
    app = create_app(settings)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(boom), base_url="http://upstream"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://shim"
    ) as c:
        resp = await c.post(
            "/v1/chat/completions",
            json={"model": "gemma4-26b", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 502
    assert resp.json()["error"]["type"] == "upstream_error"


async def test_streaming_chunks_carry_the_client_facing_model(
    client: httpx.AsyncClient, recorder: UpstreamRecorder
) -> None:
    chunks: list[dict] = []
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "gemma4-26b",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    ) as resp:
        assert resp.status_code == 200
        saw_done = False
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            body = line[len("data:") :].strip()
            if body == "[DONE]":
                saw_done = True
                continue
            chunks.append(json.loads(body))

    assert recorder.last_payload["model"] == UPSTREAM_MODEL
    assert saw_done
    assert chunks and all(c["model"] == "gemma4-26b" for c in chunks)
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert text == "hello from upstream"


async def test_models_listing_advertises_the_alias(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/models")
    assert resp.status_code == 200
    ids = [m["id"] for m in resp.json()["data"]]
    assert UPSTREAM_MODEL in ids
    assert "gemma4-26b" in ids


async def test_health_reports_upstream_and_aliases(client: httpx.AsyncClient) -> None:
    body = (await client.get("/health")).json()
    assert body["status"] == "ok"
    assert body["model_aliases"]["gemma4-26b"] == UPSTREAM_MODEL


# --- Same shim, driven through the real OpenAI SDK ---------------------------


@pytest.fixture
def sdk(shim: FastAPI) -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key="test-key",
        base_url="http://shim/v1",
        http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=shim)),
    )


async def test_sdk_create(sdk: AsyncOpenAI, recorder: UpstreamRecorder) -> None:
    completion = await sdk.chat.completions.create(
        model="gemma4-26b", messages=[{"role": "user", "content": "hi"}]
    )
    assert recorder.last_payload["model"] == UPSTREAM_MODEL
    assert completion.model == "gemma4-26b"
    assert completion.choices[0].message.content == "hello from upstream"
    assert completion.usage.total_tokens == 16


async def test_sdk_create_streaming(sdk: AsyncOpenAI) -> None:
    stream = await sdk.chat.completions.create(
        model="gemma4-26b", messages=[{"role": "user", "content": "hi"}], stream=True
    )
    text = ""
    async for chunk in stream:
        assert chunk.model == "gemma4-26b"
        text += chunk.choices[0].delta.content or ""
    assert text == "hello from upstream"


class Weather(BaseModel):
    city: str
    temperature_c: float


async def test_sdk_parse(sdk: AsyncOpenAI, recorder: UpstreamRecorder) -> None:
    completion = await sdk.chat.completions.parse(
        model="gemma4-26b",
        messages=[{"role": "user", "content": "weather in Taipei?"}],
        response_format=Weather,
    )
    # The schema reaches the upstream untouched; only the model name changed.
    assert recorder.last_payload["response_format"]["type"] == "json_schema"
    assert recorder.last_payload["model"] == UPSTREAM_MODEL
    parsed = completion.choices[0].message.parsed
    assert isinstance(parsed, Weather)
    assert parsed.city == "Taipei"
    assert parsed.temperature_c == 31.5
