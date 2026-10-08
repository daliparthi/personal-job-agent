"""Optional external chat engines for tailoring and cover letters: Ollama, OpenAI-compatible servers, Anthropic.

The page never talks to them directly: it asks this server, which adds the API key (read from the OS keychain or
.env, see envfile.api_key) and streams the reply back as plain text. The bundled Qwen model stays the default;
this module is used only when Settings > AI engine names one of ENGINES.
"""
import json
from urllib.parse import urlsplit

import httpx

ENGINES = ("ollama", "openai", "anthropic")
DEFAULT_URLS = {
    "ollama": "http://localhost:11434",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
}
ANTHROPIC_VERSION = "2023-06-01"
TIMEOUT = httpx.Timeout(connect=10, read=300, write=30, pool=10)


class LlmError(Exception):
    """Something the user can act on: unreachable server, bad key, unknown model."""


def base_url(engine: str, url: str | None) -> str:
    """The server's address without a trailing slash; http and https only."""
    url = (url or "").strip() or DEFAULT_URLS[engine]
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise LlmError("The AI server address must start with http:// or https://")
    return url.rstrip("/")


def _headers(engine: str, key: str) -> dict:
    if engine == "openai":
        return {"Authorization": f"Bearer {key}"} if key else {}
    if engine == "anthropic":
        return {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
    return {}


def _need_key(engine: str, key: str):
    if engine in ("openai", "anthropic") and not key:
        name = "OPENAI_API_KEY" if engine == "openai" else "ANTHROPIC_API_KEY"
        raise LlmError(f"No API key: save it in Settings, or put {name}=... in your .env file")


def _explain(engine: str, r: httpx.Response) -> str:
    try:
        body = r.json()
        detail = body.get("error") or body.get("message") or ""
        if isinstance(detail, dict):
            detail = detail.get("message", "")
    except Exception:
        detail = ""
    hint = " (check the API key)" if r.status_code in (401, 403) else ""
    return f"{engine} answered {r.status_code}{hint}: {str(detail)[:200]}".rstrip(": ")


async def list_models(engine: str, url: str | None, key: str = "", transport=None) -> list[str]:
    base = base_url(engine, url)
    _need_key(engine, key)
    path = {"ollama": "/api/tags", "openai": "/models", "anthropic": "/v1/models"}[engine]
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as c:
            r = await c.get(base + path, headers=_headers(engine, key))
    except httpx.HTTPError as e:
        raise LlmError(f"Cannot reach {base}: {e.__class__.__name__}") from e
    if r.status_code != 200:
        raise LlmError(_explain(engine, r))
    data = r.json()
    items = data.get("models") if engine == "ollama" else data.get("data")
    names = [(m.get("name") if engine == "ollama" else m.get("id")) for m in items or []]
    return sorted(n for n in names if n)


def _payload(engine, model, messages, temperature, max_tokens):
    if engine == "ollama":
        # think: false -- a "thinking" model would spend the short token limit on reasoning and answer with nothing
        return "/api/chat", {"model": model, "messages": messages, "stream": True, "think": False,
                             "options": {"temperature": temperature, "num_predict": max_tokens}}
    if engine == "openai":
        return "/chat/completions", {"model": model, "messages": messages, "stream": True,
                                     "temperature": temperature, "max_tokens": max_tokens}
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    body = {"model": model, "messages": [m for m in messages if m["role"] != "system"], "stream": True,
            "temperature": temperature, "max_tokens": max_tokens}
    if system:
        body["system"] = system
    return "/v1/messages", body


def _delta(engine: str, line: str) -> str:
    """The text in one streamed line (NDJSON for Ollama, server-sent events for the others)."""
    line = line.strip()
    if engine != "ollama":
        if not line.startswith("data:"):
            return ""
        line = line[5:].strip()
    if not line or line == "[DONE]":
        return ""
    try:
        d = json.loads(line)
    except ValueError:
        return ""
    if engine == "ollama":
        return (d.get("message") or {}).get("content") or ""
    if engine == "openai":
        choices = d.get("choices") or [{}]
        return (choices[0].get("delta") or {}).get("content") or ""
    if d.get("type") != "content_block_delta":
        return ""
    return (d.get("delta") or {}).get("text") or ""


async def stream_chat(engine, url, model, key, messages, temperature=0.2, max_tokens=160, transport=None):
    """Yield the reply's text pieces. LlmError is raised before the first piece when the server refuses."""
    base = base_url(engine, url)
    _need_key(engine, key)
    if not model:
        raise LlmError("Choose a model in Settings > AI engine")
    path, body = _payload(engine, model, messages, temperature, max_tokens)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as c:
            for attempt in (1, 2):
                async with c.stream("POST", base + path, json=body, headers=_headers(engine, key)) as r:
                    if r.status_code != 200:
                        await r.aread()
                        if attempt == 1 and r.status_code == 400 and "think" in body and "think" in r.text.lower():
                            del body["think"]  # an older Ollama or a model that cannot think: ask without the flag
                            continue
                        raise LlmError(_explain(engine, r))
                    async for line in r.aiter_lines():
                        piece = _delta(engine, line)
                        if piece:
                            yield piece
                    return
    except httpx.HTTPError as e:
        raise LlmError(f"Cannot reach {base}: {e.__class__.__name__}") from e
