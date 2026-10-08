import asyncio
import json

import httpx
import pytest

from app import envfile, llm

MESSAGES = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Hi"}]


def run(coro):
    return asyncio.run(coro)


async def collect(agen):
    return [p async for p in agen]


def transport(handler):
    return httpx.MockTransport(handler)


def test_base_url_defaults_and_rejects_other_schemes():
    assert llm.base_url("ollama", "") == "http://localhost:11434"
    assert llm.base_url("openai", " https://example.test/v1/ ") == "https://example.test/v1"
    for bad in ("file:///etc/passwd", "ftp://host", "localhost:11434", "http://"):
        with pytest.raises(llm.LlmError):
            llm.base_url("ollama", bad)


def test_ollama_models_and_streaming():
    seen = {}

    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}, {"name": "llama3.1:8b"}]})
        seen["body"] = json.loads(request.content)
        lines = [{"message": {"content": "Hel"}, "done": False}, {"message": {"content": "lo"}, "done": False},
                 {"message": {"content": ""}, "done": True}]
        return httpx.Response(200, content="\n".join(json.dumps(x) for x in lines))

    assert run(llm.list_models("ollama", None, transport=transport(handler))) == ["llama3.1:8b", "qwen3:8b"]
    out = run(collect(llm.stream_chat("ollama", None, "qwen3:8b", "", MESSAGES, 0.3, 90, transport=transport(handler))))
    assert "".join(out) == "Hello"
    assert seen["body"]["options"] == {"temperature": 0.3, "num_predict": 90} and seen["body"]["stream"] is True
    assert seen["body"]["think"] is False  # a thinking model must not spend the token limit on reasoning


def test_ollama_retries_without_think_when_the_model_rejects_it():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        if "think" in bodies[-1]:
            return httpx.Response(400, json={"error": "\"tiny\" does not support thinking"})
        return httpx.Response(200, content=json.dumps({"message": {"content": "ok"}, "done": True}))

    out = run(collect(llm.stream_chat("ollama", None, "tiny", "", MESSAGES, transport=transport(handler))))
    assert "".join(out) == "ok" and len(bodies) == 2 and "think" not in bodies[1]


def test_openai_compatible_streaming_sends_the_key():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["path"] = request.url.path
        sse = ['data: {"choices":[{"delta":{"role":"assistant"}}]}', 'data: {"choices":[{"delta":{"content":"Hi"}}]}',
               'data: {"choices":[{"delta":{"content":" there"}}]}', "data: [DONE]"]
        return httpx.Response(200, content="\n\n".join(sse))

    out = run(collect(llm.stream_chat("openai", "https://api.test/v1", "m", "sk-test", MESSAGES, transport=transport(handler))))
    assert "".join(out) == "Hi there"
    assert seen == {"auth": "Bearer sk-test", "path": "/v1/chat/completions"}


def test_anthropic_streaming_moves_the_system_prompt():
    seen = {}

    def handler(request):
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        sse = ['event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Ok"}}',
               'event: message_start\ndata: {"type":"message_start"}',
               'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"!"}}']
        return httpx.Response(200, content="\n\n".join(sse))

    out = run(collect(llm.stream_chat("anthropic", None, "m", "key-1", MESSAGES, transport=transport(handler))))
    assert "".join(out) == "Ok!"
    assert seen["headers"]["x-api-key"] == "key-1" and "anthropic-version" in seen["headers"]
    assert seen["body"]["system"] == "Be brief." and [m["role"] for m in seen["body"]["messages"]] == ["user"]


def test_errors_are_explained():
    def refuse(request):
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    def down(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(llm.LlmError, match="check the API key"):
        run(collect(llm.stream_chat("openai", None, "m", "k", MESSAGES, transport=transport(refuse))))
    with pytest.raises(llm.LlmError, match="Cannot reach"):
        run(llm.list_models("ollama", None, transport=transport(down)))
    with pytest.raises(llm.LlmError, match="No API key"):
        run(llm.list_models("openai", None, "", transport=transport(refuse)))
    with pytest.raises(llm.LlmError, match="Choose a model"):
        run(collect(llm.stream_chat("ollama", None, "", "", MESSAGES)))


def test_api_keys_live_in_the_keychain_and_are_never_in_status():
    envfile.store_api_key("openai", "sk-secret")
    assert envfile.api_key("openai") == "sk-secret" and envfile.api_key("anthropic") == ""
    st = envfile.status()
    assert st["api_keys"] == {"openai": True, "anthropic": False} and "sk-secret" not in json.dumps(st)
    envfile.store_api_key("openai", "")
    assert envfile.api_key("openai") == ""


# ---------------------------------------------------------------- the local API
def test_chat_endpoint_requires_an_external_engine_and_streams(client, monkeypatch):
    assert client.post("/api/llm/chat", json={"messages": MESSAGES}).status_code == 400  # engine is still "auto"
    client.put("/api/settings", json={"engine": "ollama", "llm_model": "m"})

    async def fake_stream(engine, url, model, key, messages, temperature, max_tokens):
        assert (engine, model, max_tokens) == ("ollama", "m", 50)
        yield "Hel"
        yield "lo"

    async def fake_models(engine, url, key):
        return ["m"]

    monkeypatch.setattr(llm, "stream_chat", fake_stream)
    monkeypatch.setattr(llm, "list_models", fake_models)
    r = client.post("/api/llm/chat", json={"messages": MESSAGES, "max_tokens": 50})
    assert r.status_code == 200 and r.text == "Hello"
    assert client.get("/api/llm/models").json() == {"engine": "ollama", "models": ["m"]}
    client.put("/api/settings", json={"engine": "auto"})


def test_chat_endpoint_reports_a_refusal_before_streaming(client, monkeypatch):
    client.put("/api/settings", json={"engine": "openai", "llm_model": "m"})

    async def refuse(*args, **kwargs):
        raise llm.LlmError("openai answered 401 (check the API key)")
        yield ""

    monkeypatch.setattr(llm, "stream_chat", refuse)
    r = client.post("/api/llm/chat", json={"messages": MESSAGES})
    assert r.status_code == 502 and "API key" in r.json()["detail"]
    client.put("/api/settings", json={"engine": "auto"})


def test_key_endpoint_stores_and_clears(client):
    assert client.post("/api/llm/key", json={"engine": "anthropic", "key": "abc"}).json()["api_keys"]["anthropic"] is True
    assert client.post("/api/llm/key", json={"engine": "anthropic", "key": ""}).json()["api_keys"]["anthropic"] is False
    assert client.post("/api/llm/key", json={"engine": "ollama", "key": "x"}).status_code == 422
