"""A stand-in for the real upstream at http://192.168.x.x:8000.

It mimics the parts of that server the shim touches: bearer auth, a model
listing, and chat completions (streaming and not), including honouring a
``response_format`` of type ``json_schema`` the way ``.parse()`` needs.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.config import Settings
from app.main import create_app

UPSTREAM_MODEL = "Gemma-4-26B-A4B"


class UpstreamRecorder:
    """Captures what the upstream actually received."""

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []

    @property
    def last_payload(self) -> dict[str, Any]:
        return self.payloads[-1]

    @property
    def last_headers(self) -> dict[str, str]:
        return self.headers[-1]


def build_upstream(recorder: UpstreamRecorder) -> FastAPI:
    upstream = FastAPI()

    def unauthorized() -> JSONResponse:
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    @upstream.get("/v1/models")
    async def models(request: Request) -> JSONResponse:
        if not request.headers.get("authorization"):
            return unauthorized()
        return JSONResponse(
            {
                "object": "list",
                "data": [{"id": UPSTREAM_MODEL, "object": "model", "owned_by": "local"}],
            }
        )

    @upstream.post("/v1/chat/completions")
    async def chat(request: Request) -> Any:
        payload = await request.json()
        recorder.payloads.append(payload)
        recorder.headers.append(dict(request.headers))

        if not request.headers.get("authorization"):
            return unauthorized()
        if payload["model"] != UPSTREAM_MODEL:
            return JSONResponse(
                status_code=404,
                content={"error": {"message": f"model {payload['model']} not found"}},
            )

        # `.parse()` sends a json_schema response_format; answer with matching JSON.
        fmt = payload.get("response_format") or {}
        if fmt.get("type") == "json_schema":
            content = json.dumps({"city": "Taipei", "temperature_c": 31.5})
        else:
            content = "hello from upstream"

        if payload.get("stream"):
            def sse() -> Any:
                base = {
                    "id": "chatcmpl-stream",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": UPSTREAM_MODEL,
                }
                for piece in (content[: len(content) // 2], content[len(content) // 2 :]):
                    chunk = dict(base, choices=[
                        {"index": 0, "delta": {"content": piece}, "finish_reason": None}
                    ])
                    yield f"data: {json.dumps(chunk)}\n\n"
                final = dict(base, choices=[
                    {"index": 0, "delta": {}, "finish_reason": "stop"}
                ])
                yield f"data: {json.dumps(final)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(sse(), media_type="text/event-stream")

        return JSONResponse(
            {
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 1,
                "model": UPSTREAM_MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content, "refusal": None},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 9, "completion_tokens": 7, "total_tokens": 16},
            }
        )

    return upstream


@pytest.fixture
def recorder() -> UpstreamRecorder:
    return UpstreamRecorder()


@pytest.fixture
def settings() -> Settings:
    return Settings(upstream_base_url="http://upstream", upstream_api_key=None)


@pytest.fixture
def shim(recorder: UpstreamRecorder, settings: Settings) -> FastAPI:
    """The shim app, wired to the fake upstream instead of the network."""
    app = create_app(settings)
    app.state.upstream = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_upstream(recorder)),
        base_url="http://upstream",
    )
    return app


@pytest.fixture
def client(shim: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=shim),
        base_url="http://shim",
        headers={"Authorization": "Bearer test-key"},
    )
