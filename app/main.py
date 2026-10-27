"""OpenAI-compatible shim in front of an upstream OpenAI-compatible server.

Serves the two endpoints the OpenAI Python SDK needs for
``client.chat.completions.create()`` and ``client.chat.completions.parse()``
(both are POST /v1/chat/completions, the latter with a ``response_format`` of
type ``json_schema``), plus GET /v1/models.

Requests are forwarded to the upstream server with the model name rewritten
through the alias table (``gemma4-26b`` -> ``Gemma-4-26B-A4B`` by default), and
the client-facing name is written back into the response so callers keep seeing
the name they asked for. The bearer token is mapped the same way through the
API key table, so callers can keep using the key of the target environment.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .config import Settings, load_settings

CHAT_PATH = "/v1/chat/completions"
MODELS_PATH = "/v1/models"

# Headers we never copy from the client to the upstream (or back): hop-by-hop
# metadata and anything httpx recomputes for the new request/response body.
_SKIP_REQUEST_HEADERS = {
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "accept-encoding",
    "upgrade",
    "te",
    "trailer",
    "proxy-authorization",
    "proxy-authenticate",
}
_SKIP_RESPONSE_HEADERS = {
    "content-length",
    "content-encoding",
    # Set by our own server; copying them would emit duplicates.
    "date",
    "server",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    "te",
    "trailer",
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    timeout = httpx.Timeout(
        settings.read_timeout,
        connect=settings.connect_timeout,
        # Streamed completions can idle between tokens; keep the read budget generous.
        read=settings.read_timeout,
    )
    app.state.upstream = httpx.AsyncClient(
        base_url=settings.upstream_base_url,
        timeout=timeout,
        follow_redirects=True,
    )
    try:
        yield
    finally:
        await app.state.upstream.aclose()


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="openai-compat-shim", lifespan=lifespan)
    app.state.settings = settings or load_settings()
    _register_routes(app)
    return app


def _error(status: int, message: str, err_type: str = "invalid_request_error") -> JSONResponse:
    """An OpenAI-shaped error body, so SDK error classes still make sense."""
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": err_type, "param": None, "code": None}},
    )


def _forward_headers(request: Request, settings: Settings) -> dict[str, str]:
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _SKIP_REQUEST_HEADERS and k.lower() != "authorization"
    }
    auth = request.headers.get("authorization")
    if auth:
        scheme, _, token = auth.partition(" ")
        if scheme.lower() == "bearer" and token:
            auth = f"Bearer {settings.resolve_api_key(token.strip())}"
        headers["Authorization"] = auth
    elif settings.upstream_api_key:
        headers["Authorization"] = f"Bearer {settings.upstream_api_key}"
    return headers


def _response_headers(upstream: httpx.Response) -> dict[str, str]:
    return {
        k: v for k, v in upstream.headers.items() if k.lower() not in _SKIP_RESPONSE_HEADERS
    }


def _passthrough(upstream: httpx.Response) -> Response:
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=_response_headers(upstream),
        media_type=upstream.headers.get("content-type"),
    )


def _rewrite_sse_line(line: str, client_model: str) -> str:
    """Put the client-facing model name back into one `data:` line of an SSE stream."""
    if not line.startswith("data:"):
        return line
    payload = line[len("data:") :].strip()
    if not payload or payload == "[DONE]":
        return line
    try:
        chunk = json.loads(payload)
    except json.JSONDecodeError:
        return line
    if isinstance(chunk, dict) and "model" in chunk:
        chunk["model"] = client_model
        return "data: " + json.dumps(chunk, ensure_ascii=False)
    return line


def _register_routes(app: FastAPI) -> None:
    @app.get("/health")
    async def health() -> dict[str, Any]:
        settings: Settings = app.state.settings
        return {
            "status": "ok",
            "upstream": settings.upstream_base_url,
            "model_aliases": settings.model_aliases,
        }

    @app.get(MODELS_PATH)
    async def list_models(request: Request) -> Response:
        settings: Settings = app.state.settings
        client: httpx.AsyncClient = app.state.upstream
        try:
            upstream = await client.get(MODELS_PATH, headers=_forward_headers(request, settings))
        except httpx.HTTPError as exc:
            return _error(502, f"upstream request failed: {exc}", "upstream_error")

        if upstream.status_code != 200:
            return _passthrough(upstream)

        try:
            body = upstream.json()
        except ValueError:
            return _passthrough(upstream)

        # Advertise the aliases too, so `gemma4-26b` shows up in a model listing.
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, list):
            known = {m.get("id") for m in data if isinstance(m, dict)}
            for alias, target in settings.model_aliases.items():
                if alias in known:
                    continue
                source = next(
                    (m for m in data if isinstance(m, dict) and m.get("id") == target), None
                )
                entry = dict(source) if source else {"object": "model", "owned_by": "shim"}
                entry["id"] = alias
                data.append(entry)
        return JSONResponse(content=body, status_code=200)

    @app.post(CHAT_PATH)
    async def chat_completions(request: Request) -> Response:
        settings: Settings = app.state.settings
        client: httpx.AsyncClient = app.state.upstream

        raw = await request.body()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return _error(400, "request body must be valid JSON")
        if not isinstance(payload, dict):
            return _error(400, "request body must be a JSON object")

        client_model = payload.get("model")
        if not isinstance(client_model, str) or not client_model:
            return _error(400, "'model' is required and must be a string")

        payload["model"] = settings.resolve_model(client_model)
        headers = _forward_headers(request, settings)
        streaming = bool(payload.get("stream"))

        if not streaming:
            try:
                upstream = await client.post(CHAT_PATH, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                return _error(502, f"upstream request failed: {exc}", "upstream_error")

            if upstream.status_code != 200:
                return _passthrough(upstream)
            try:
                body = upstream.json()
            except ValueError:
                return _passthrough(upstream)
            if isinstance(body, dict) and "model" in body:
                body["model"] = client_model
            return JSONResponse(content=body, status_code=200)

        req = client.build_request("POST", CHAT_PATH, json=payload, headers=headers)
        try:
            upstream = await client.send(req, stream=True)
        except httpx.HTTPError as exc:
            return _error(502, f"upstream request failed: {exc}", "upstream_error")

        if upstream.status_code != 200:
            await upstream.aread()
            await upstream.aclose()
            return _passthrough(upstream)

        async def relay() -> AsyncIterator[bytes]:
            try:
                async for line in upstream.aiter_lines():
                    yield (_rewrite_sse_line(line, client_model) + "\n").encode("utf-8")
            finally:
                await upstream.aclose()

        return StreamingResponse(
            relay(),
            status_code=200,
            media_type=upstream.headers.get("content-type", "text/event-stream"),
        )


app = create_app()
