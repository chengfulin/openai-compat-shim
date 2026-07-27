"""Drive the shim with the real OpenAI SDK.

    OPENAI_API_KEY=<upstream key> .venv/bin/python examples/client_demo.py
"""

from __future__ import annotations

import os

from openai import OpenAI
from pydantic import BaseModel

client = OpenAI(
    base_url=os.getenv("SHIM_BASE_URL", "http://127.0.0.1:8000/v1"),
    api_key=os.getenv("OPENAI_API_KEY", "dummy"),
)

MODEL = "gemma4-26b"  # the shim rewrites this to Gemma-4-26B-A4B


def demo_create() -> None:
    completion = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": "用一句話介紹台北。"}],
    )
    print("[create]", completion.model, "->", completion.choices[0].message.content)


def demo_stream() -> None:
    print("[stream] ", end="", flush=True)
    for chunk in client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": "從 1 數到 5。"}],
        stream=True,
    ):
        print(chunk.choices[0].delta.content or "", end="", flush=True)
    print()


class Weather(BaseModel):
    city: str
    temperature_c: float


def demo_parse() -> None:
    completion = client.chat.completions.parse(
        model=MODEL,
        messages=[{"role": "user", "content": "台北今天 31.5 度，整理成結構化資料。"}],
        response_format=Weather,
    )
    print("[parse]", completion.choices[0].message.parsed)


if __name__ == "__main__":
    demo_create()
    demo_stream()
    demo_parse()
