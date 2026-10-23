"""The voice page, the settings page and their API. Each API call carries the token the page
holds; the guard in routes keeps other websites out."""

from __future__ import annotations

import asyncio
import hmac
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import checks, engines, settings
from .languages import languages as speech_languages
from .settings import (
    APPS,
    FIELDS,
    KEYS,
    LLM,
    MODELS,
    PROCESS,
    SECRETS,
    SERVER,
    STT,
    TTS,
    VOICES,
    SettingsError,
)

PAGES = {"/voice": "voice.html", "/settings": "settings.html"}
TOKEN_HEADER = "x-voice-relay"
CACHE_SECONDS = 600.0
KEY_LINKS = {
    "ELEVENLABS_API_KEY": ["ElevenLabs", "https://elevenlabs.io/app/settings/api-keys"],
    "OPENROUTER_API_KEY": ["OpenRouter", "https://openrouter.ai/keys"],
    "DEEPGRAM_API_KEY": ["Deepgram", "https://console.deepgram.com/"],
    "ASSEMBLYAI_API_KEY": ["AssemblyAI", "https://www.assemblyai.com/dashboard"],
    "CARTESIA_API_KEY": ["Cartesia", "https://play.cartesia.ai/keys"],
    "OPENAI_API_KEY": ["OpenAI", "https://platform.openai.com/api-keys"],
    "ANTHROPIC_API_KEY": ["Anthropic", "https://console.anthropic.com/settings/keys"],
    "GOOGLE_API_KEY": ["Google", "https://aistudio.google.com/apikey"],
    "GROQ_API_KEY": ["Groq", "https://console.groq.com/keys"],
}
CATALOG = {  # provider: name, where it runs (api, local or server), what to know
    "elevenlabs": {"stt": ("ElevenLabs Scribe", "api", "Detects the language by itself."),
                   "tts": ("ElevenLabs", "api", "Natural voices in many languages.")},
    "deepgram": {"stt": ("Deepgram", "api", "Nova-3. Set the language for non-English speech."),
                 "tts": ("Deepgram", "api", "Aura voices.")},
    "assemblyai": {"stt": ("AssemblyAI", "api", "Fast turn-taking.")},
    "cartesia": {"stt": ("Cartesia", "api", "Ink speech recognition."),
                 "tts": ("Cartesia", "api", "Sonic voices. Lowest latency.")},
    "openai": {"stt": ("OpenAI", "api", "Transcribes after you stop talking."),
               "llm": ("OpenAI", "api", "GPT models, direct."),
               "tts": ("OpenAI", "api", "A small set of clear voices.")},
    "openrouter": {"llm": ("OpenRouter", "api", "Hundreds of models with one key.")},
    "anthropic": {"llm": ("Anthropic", "api", "Claude models, direct.")},
    "google": {"llm": ("Google", "api", "Gemini models, direct.")},
    "groq": {"llm": ("Groq", "api", "Open models at high speed.")},
    "moonshine": {"stt": ("Moonshine", "local", "Fast on a CPU. 10 languages.")},
    "whisper": {"stt": ("Whisper", "local", "99 languages. A bigger model hears better and "
                        "needs a faster computer.")},
    "kokoro": {"tts": ("Kokoro", "local", "The most natural local voices. English.")},
    "piper": {"tts": ("Piper", "local", "Light and fast. Voices in 50 languages.")},
    "pocket": {"tts": ("Pocket TTS", "local", "Kyutai voices, English. Brings PyTorch: 600 MB.")},
    "ollama": {"llm": ("Ollama", "local", "A separate app on this computer.")},
    "lmstudio": {"llm": ("LM Studio", "local", "A separate app on this computer.")},
    "llamacpp": {"llm": ("llama.cpp", "local", "llama-server on this computer.")},
    SERVER: {"stt": ("Your server", "server", "OpenAI transcription API, for example speaches."),
             "llm": ("Your server", "server", "OpenAI chat API: vLLM, SGLang, llama.cpp."),
             "tts": ("Your server", "server", "OpenAI speech API, for example Kokoro-FastAPI.")},
}  # fmt: skip
GUIDE = "https://github.com/softcery/claude-code-voice-chat#llm-apps-on-this-computer"
ABOUT = {  # the detail behind the info icon: what the operator installs and runs
    "ollama": "Install Ollama and pull a model that calls tools, for example with ollama pull. "
    "Keep Ollama running. voice-relay talks to it on port 11434.",
    "lmstudio": "Install LM Studio and download a model that calls tools. In the Developer tab, "
    "load the model and start the server. voice-relay talks to it on port 1234.",
    "llamacpp": "Install llama.cpp and start llama-server with a model and --jinja, which "
    "turns on tool calls. voice-relay talks to it on port 8080.",
}
OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"
ELEVENLABS_VOICES = "https://api.elevenlabs.io/v2/voices?page_size=100"
CARTESIA_VOICES = "https://api.cartesia.ai/voices?limit=100"
STATIC_VOICES = {
    "openai": ["alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage"],
    "deepgram": ["aura-2-thalia-en", "aura-2-helena-en", "aura-2-andromeda-en", "aura-2-apollo-en"],
    "kokoro": [
        "af_heart", "af_bella", "af_nicole", "af_sarah", "af_sky", "am_michael", "am_adam",
        "bf_emma", "bf_isabella", "bm_daniel", "bm_fable",
    ],
    "pocket": list(engines.POCKET_VOICES),
}  # fmt: skip

_cache: dict[str, tuple[float, Any]] = {}
_installs = engines.Installs()


def mount(app: FastAPI, *, token: str) -> None:

    def guard(request: Request) -> None:
        if not hmac.compare_digest(request.headers.get(TOKEN_HEADER, ""), token):
            raise HTTPException(status_code=403, detail="settings: expected the page token")

    def page(name: str) -> Callable[[], Awaitable[HTMLResponse]]:
        async def serve() -> HTMLResponse:
            html = (Path(__file__).parent / "web" / name).read_text(encoding="utf-8")
            return HTMLResponse(html.replace("{{TOKEN}}", token))

        return serve

    for path, name in PAGES.items():
        app.get(path, include_in_schema=False)(page(name))

    @app.get("/settings/state")
    async def state(request: Request) -> JSONResponse:
        guard(request)
        return JSONResponse(_state())

    @app.post("/settings/state")
    async def update(request: Request) -> JSONResponse:
        guard(request)
        values = await request.json()
        if not isinstance(values, dict) or not all(isinstance(v, str) for v in values.values()):
            raise HTTPException(status_code=400, detail="body: expected an object of strings")
        try:
            settings.save(values)
        except SettingsError as error:
            raise HTTPException(status_code=400, detail=str(error)) from None
        return JSONResponse(_state())

    @app.get("/settings/voices")
    async def voices(request: Request, provider: str) -> JSONResponse:
        guard(request)
        return JSONResponse(await _voices(provider))

    @app.get("/settings/models")
    async def models(request: Request) -> JSONResponse:
        guard(request)
        return JSONResponse(await _models())

    @app.get("/settings/languages")
    async def languages(request: Request, provider: str) -> JSONResponse:
        """The languages a speech recognition provider takes, from its own source."""
        guard(request)
        env = settings.environment()
        model = (
            env.get("VOICE_RELAY_STT_MODEL", "") if env.get("VOICE_RELAY_STT") == provider else ""
        )
        key = env.get(KEYS.get(provider, ""), "")
        return JSONResponse(await speech_languages(provider, model=model, key=key))

    @app.post("/settings/test")
    async def test(request: Request) -> JSONResponse:
        """Tests one saved stage, stt, llm or tts, with real input."""
        guard(request)
        stage = (await request.json()).get("stage")
        if stage not in ("stt", "llm", "tts"):
            raise HTTPException(400, "stage: expected stt, llm or tts")
        current = _load()
        key = KEYS.get(getattr(current, stage).provider, "-")
        known = [
            p for p in settings.problems() if p.startswith((f"VOICE_RELAY_{stage.upper()}", key))
        ]
        if known:  # no need to ask the provider what is already missing here
            return JSONResponse({"ok": False, "summary": "; ".join(known)})
        return JSONResponse(await checks.check(stage, current))

    @app.post("/settings/server-models")
    async def server_models(request: Request) -> JSONResponse:
        """Lists the models an operator's server serves, from the url and key on the page."""
        guard(request)
        body = await request.json()
        url, key = str(body.get("url") or "").rstrip("/"), str(body.get("key") or "")
        if not key and body.get("stage") in ("stt", "llm", "tts"):  # the saved key of the stage
            key = settings.environment().get(f"VOICE_RELAY_{body['stage'].upper()}_KEY", "")
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                headers = {"authorization": f"Bearer {key}"} if key else {}
                reply = await client.get(f"{url}/models", headers=headers)
                reply.raise_for_status()
            ids = [row["id"] for row in reply.json().get("data", []) if isinstance(row, dict)]
        except (httpx.HTTPError, ValueError, KeyError, AttributeError) as error:
            return JSONResponse(
                {"models": [], "error": f"no model list at {url}/models: {type(error).__name__}"}
            )
        return JSONResponse({"models": ids})

    @app.post("/settings/local")
    async def local_model(request: Request) -> JSONResponse:
        """Installs an engine with the model from the settings, or removes its models. Refuses
        to remove one in use."""
        guard(request)
        body = await request.json()
        provider, action = body.get("provider"), body.get("action")
        if provider not in engines.ENGINES or action not in ("install", "remove"):
            raise HTTPException(400, "body: expected a local engine and install or remove")
        current = _load()
        if action == "install":
            _installs.start(provider, engines.variant(provider, settings.environment()))
        elif provider in (current.stt.provider, current.tts.provider):
            raise HTTPException(409, f"{provider}: in use; pick another provider first")
        elif _installs.busy():  # removal syncs the packages: it must not cut an install
            raise HTTPException(409, f"{provider}: an install runs; remove it when it ends")
        else:
            await asyncio.to_thread(engines.remove, provider)
        return JSONResponse(_state())

    @app.post("/settings/engine")
    async def engine(request: Request) -> JSONResponse:
        """The state and download size of an engine with the values on the page, unsaved."""
        guard(request)
        body = await request.json()
        provider, values = body.get("provider"), body.get("values") or {}
        if provider not in engines.ENGINES or not isinstance(values, dict):
            raise HTTPException(400, "body: expected a local engine and an object of values")
        chosen = engines.ENGINES[provider]
        env = {**settings.environment(), **values, f"VOICE_RELAY_{chosen.stage.upper()}": provider}
        return JSONResponse(await asyncio.to_thread(_engine, chosen, env))


def _load() -> settings.Settings:
    try:
        return settings.load()
    except SettingsError as error:
        raise HTTPException(400, str(error)) from None


def _state() -> dict[str, Any]:
    env = settings.environment()
    return {
        "values": {name: env.get(name, "") for name in FIELDS} | settings.texts(),
        "builtin_prompt": settings.builtin(),
        "prompt_outdated": settings.outdated(),
        "locked": [name for name in (*FIELDS, *SECRETS) if PROCESS.get(name)],
        "keys": {name: bool(env.get(name)) for name in SECRETS},
        "providers": {"stt": STT, "llm": LLM, "tts": TTS},
        "catalog": {
            stage: {
                name: dict(zip(("name", "where", "hint"), CATALOG[name][stage], strict=True))
                | (
                    {"about": ABOUT[name], "guide": GUIDE, "url": APPS[name]}
                    if name in APPS
                    else {}
                )
                for name in names
            }
            for stage, names in (("stt", STT), ("llm", LLM), ("tts", TTS))
        },
        "key_names": KEYS,
        "key_links": KEY_LINKS,
        "problems": settings.problems(env),
        "recommended": MODELS["openrouter"],
        "default_models": MODELS,
        "voice_defaults": VOICES,
        "local": {name: _engine(engine, env) for name, engine in engines.ENGINES.items()},
        "whisper_models": engines.WHISPER_MB,
        "machine": engines.machine(),
    }


def _engine(engine: engines.Engine, env: dict[str, str]) -> dict[str, Any]:
    chosen = engines.variant(engine.provider, env)
    return {
        "provider": engine.provider,
        "stage": engine.stage,
        "title": CATALOG[engine.provider][engine.stage][0],
        "variant": chosen,
        "megabytes": engines.need_mb(engine.provider, chosen),
        **_installs.state(engine.provider, chosen),
    }


async def _voices(provider: str) -> dict[str, Any]:
    env = settings.environment()
    key = env.get(KEYS.get(provider, ""), "")
    if provider == "piper":
        try:
            return {"voices": await asyncio.to_thread(engines.piper_voices)}
        except (httpx.HTTPError, ValueError) as error:
            return {"voices": [], "error": f"Piper did not list its voices: {type(error).__name__}"}
    if provider in STATIC_VOICES:
        return {"voices": [{"id": name, "name": name} for name in STATIC_VOICES[provider]]}
    if not key:
        return {"voices": [], "error": f"Add the {provider} API key to list its voices."}
    try:
        if provider == "elevenlabs":
            data = await _get(ELEVENLABS_VOICES, {"xi-api-key": key}, key)
            return {"voices": [_elevenlabs(voice) for voice in data.get("voices", [])]}
        if provider == "cartesia":
            headers = {"X-API-Key": key, "Cartesia-Version": "2025-04-16"}
            data = await _get(CARTESIA_VOICES, headers, key)
            rows = data.get("data", data) if isinstance(data, dict) else data
            return {"voices": [_cartesia(voice) for voice in rows]}
    except httpx.HTTPError as error:
        return {
            "voices": [],
            "error": f"{provider} did not list its voices: {type(error).__name__}",
        }
    return {"voices": []}


async def _models() -> dict[str, Any]:
    try:
        data = await _get(OPENROUTER_MODELS, {}, "")
    except httpx.HTTPError as error:
        return {"models": [], "error": f"OpenRouter did not list models: {type(error).__name__}"}
    rows = []
    for model in data.get("data", []):
        price = model.get("pricing", {})
        if "tools" not in (model.get("supported_parameters") or []):
            continue
        if model["id"].endswith(":free") or model["id"].endswith(":batch"):
            continue
        rows.append(
            {
                "id": model["id"],
                "name": model.get("name", model["id"]),
                "input": float(price.get("prompt") or 0) * 1e6,
                "output": float(price.get("completion") or 0) * 1e6,
                "created": model.get("created", 0),
            }
        )
    rows.sort(key=lambda row: (row["id"] != MODELS["openrouter"], -row["created"]))
    return {"models": rows}


async def _get(url: str, headers: dict[str, str], key: str) -> Any:
    cached = _cache.get(url + key[-6:])
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    async with httpx.AsyncClient(timeout=15) as client:
        reply = await client.get(url, headers=headers)
        reply.raise_for_status()
        data = reply.json()
    _cache[url + key[-6:]] = (time.monotonic(), data)
    return data


def _elevenlabs(voice: dict[str, Any]) -> dict[str, Any]:
    labels = voice.get("labels") or {}
    traits = [labels.get(name) for name in ("gender", "age", "accent", "descriptive")]
    return {
        "id": voice.get("voice_id"),
        "name": voice.get("name"),
        "detail": ", ".join(trait for trait in traits if trait),
        "preview": voice.get("preview_url"),
    }


def _cartesia(voice: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": voice.get("id"),
        "name": voice.get("name"),
        "detail": ", ".join(
            part for part in (voice.get("language"), voice.get("description")) if part
        ),
    }
