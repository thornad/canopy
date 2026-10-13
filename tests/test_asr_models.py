"""The transcription picker lists only models oMLX runs as speech-to-text."""

import httpx
import pytest

from canopy import server

# oMLX's own classification of the models on this machine (2026-10-13).
STATUS = {"models": [
    {"id": "GLM-ASR-Nano-2512-8bit", "engine_type": "audio_stt"},
    {"id": "VibeVoice-ASR-8bit", "engine_type": "audio_stt"},
    {"id": "VibeVoice-ASR-Streaming-7B", "engine_type": "audio_tts"},
    {"id": "vibevoice-mlx", "engine_type": "audio_tts"},
    {"id": "whisper-large-v3-turbo-8bit", "engine_type": "audio_stt"},
    {"id": "GLM-5.2-oQ4e-mtp", "engine_type": "batched"},
]}


def _serve(monkeypatch, routes):
    async def settings():
        return {}

    def handler(request):
        body = routes.get(request.url.path)
        return httpx.Response(200, json=body) if body is not None else httpx.Response(404)

    real = httpx.AsyncClient
    monkeypatch.setattr(server.db, "get_settings", settings)
    monkeypatch.setattr(server.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


async def test_only_speech_to_text_models_are_listed(monkeypatch):
    _serve(monkeypatch, {"/v1/models/status": STATUS})
    data = (await server.list_asr_models())["data"]
    assert data == [
        {"id": "GLM-ASR-Nano-2512-8bit", "max_seconds": None},
        {"id": "VibeVoice-ASR-8bit", "max_seconds": 59 * 60},
        {"id": "whisper-large-v3-turbo-8bit", "max_seconds": None},
    ]


async def test_falls_back_to_names_without_engine_type(monkeypatch):
    _serve(monkeypatch, {
        "/v1/models/status": {"models": [{"id": "whisper-x"}]},
        "/v1/models": {"data": [{"id": "whisper-x"}, {"id": "llama-3"}]},
    })
    data = (await server.list_asr_models())["data"]
    assert [m["id"] for m in data] == ["whisper-x"]
