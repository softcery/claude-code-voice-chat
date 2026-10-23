"""Stock pipecat services for one voice session, from the settings."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.processors.aggregators.llm_response_universal import (
    LLMAssistantAggregatorParams,
    LLMUserAggregatorParams,
)
from pipecat.services.llm_service import LLMService
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService
from pipecat.turns.empty_user_turn import EmptyUserTurnConfig
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.utils.context.llm_context_summarization import (
    LLMAutoContextSummarizationConfig,
    LLMContextSummaryConfig,
)

from .engines import DEFAULTS
from .settings import MODELS, SERVERS, VOICES, Service

GROQ_URL = "https://api.groq.com/openai/v1"
NO_REASONING = {"reasoning": {"enabled": False}}  # a voice reply cannot wait for thinking
MAX_TOKENS = 1000  # a cap, not a target: room for a full read-back and a reasoning model
SUMMARY_TOKENS = 2000
CONTEXT_TOKENS = 40_000  # a summary replaces the older messages above this size
WINDOW_SHARE = 0.6  # of a small server window: room for the reply and the summary call
KEPT_MESSAGES = 12
SPOKE_OVER = (
    "The operator spoke over you, and the speech recognition got no words."  # see the prompt
)
SUMMARY_PROMPT = """Summarize this voice session between an operator, a voice agent and Claude Code.
Keep, as short facts:
- each task the operator gave, with its conditions and limits;
- what Claude Code did, found and changed, with file names;
- open questions, choices Claude Code offered, and work still running or waiting;
- decisions and preferences of the operator.
Drop greetings, small talk and progress lines that a later answer replaced.
Write only the summary."""


def stt(service: Service) -> STTService:
    key = service.key
    fields = {"model": service.model, "language": service.language}
    match service.provider:
        case "elevenlabs":
            from pipecat.services.elevenlabs.stt import ElevenLabsRealtimeSTTService

            settings = ElevenLabsRealtimeSTTService.Settings(**_given(fields))
            return ElevenLabsRealtimeSTTService(api_key=key, settings=settings)
        case "assemblyai":
            from pipecat.services.assemblyai.stt import AssemblyAISTTService

            return AssemblyAISTTService(
                api_key=key, settings=AssemblyAISTTService.Settings(**_given(fields))
            )
        case "deepgram":
            from pipecat.services.deepgram.stt import DeepgramSTTService

            return DeepgramSTTService(
                api_key=key, settings=DeepgramSTTService.Settings(**_given(fields))
            )
        case "cartesia":
            from pipecat.services.cartesia.stt import CartesiaSTTService

            return CartesiaSTTService(
                api_key=key, settings=CartesiaSTTService.Settings(**_given(fields))
            )
        case "moonshine":
            from pipecat.services.moonshine.stt import MoonshineSTTService

            return MoonshineSTTService(settings=MoonshineSTTService.Settings(**_given(fields)))
        case "whisper":
            if "mlx_whisper" not in sys.modules and not importlib.util.find_spec("mlx_whisper"):
                # pipecat imports it on a Mac with Apple silicon, for an MLX service not used here
                sys.modules["mlx_whisper"] = types.ModuleType("mlx_whisper")
            from pipecat.services.whisper.stt import WhisperSTTService

            settings = WhisperSTTService.Settings(
                model=service.model or DEFAULTS["whisper"],
                language=service.language or None,  # None: Whisper detects the language
            )
            return WhisperSTTService(compute_type=_whisper_compute(), settings=settings)
        case _:  # openai, or the operator's server on its API
            from pipecat.services.openai.stt import OpenAISTTService

            settings = OpenAISTTService.Settings(**_given(fields))
            url = service.url or None
            return OpenAISTTService(api_key=key or "none", base_url=url, settings=settings)


def llm(service: Service, *, prompt: str, max_tokens: int = MAX_TOKENS) -> LLMService[Any]:
    model = service.model or MODELS.get(service.provider)
    common: dict[str, Any] = {
        "model": model,
        "system_instruction": prompt,
        "max_tokens": max_tokens,
    }
    match service.provider:
        case "anthropic":
            from pipecat.services.anthropic.llm import AnthropicLLMService

            settings = AnthropicLLMService.Settings(**_given(common), enable_prompt_caching=True)
            return AnthropicLLMService(api_key=service.key, settings=settings)
        case "google":
            from pipecat.services.google.llm import GoogleLLMService

            settings = GoogleLLMService.Settings(**_given(common))
            return GoogleLLMService(api_key=service.key, settings=settings)
        case "openrouter":
            from pipecat.services.openrouter.llm import OpenRouterLLMService

            claude = str(model).startswith("anthropic/")  # other models can refuse it
            settings = OpenRouterLLMService.Settings(
                **_given(common), extra=_body(service.body, NO_REASONING if claude else None)
            )
            return _cached(OpenRouterLLMService)(api_key=service.key, settings=settings)
        case "openai":
            from pipecat.services.openai.llm import OpenAILLMService

            settings = OpenAILLMService.Settings(**_given(common), extra=_body(service.body))
            return OpenAILLMService(api_key=service.key, settings=settings)
        case _:
            return _compatible(service, common)


def tts(service: Service, *, speed: float) -> TTSService:
    key, model = service.key, service.model or None
    voice = service.voice or VOICES.get(service.provider)
    match service.provider:
        case "cartesia":
            from pipecat.services.cartesia.tts import CartesiaTTSService, GenerationConfig

            settings = CartesiaTTSService.Settings(
                **_given({"model": model, "voice": voice}),
                generation_config=GenerationConfig(speed=speed),
            )
            return CartesiaTTSService(api_key=key, settings=settings)
        case "elevenlabs":
            from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

            settings = ElevenLabsTTSService.Settings(
                **_given({"model": model, "voice": voice}), speed=speed
            )
            return ElevenLabsTTSService(api_key=key, settings=settings)
        case "kokoro":
            from pipecat.services.kokoro.tts import KokoroTTSService

            settings = KokoroTTSService.Settings(voice=voice, speed=speed)
            return KokoroTTSService(settings=settings)
        case "piper":
            from pipecat.services.piper.tts import PiperTTSService

            return PiperTTSService(settings=PiperTTSService.Settings(voice=voice))
        case "pocket":
            from pipecat.services.pocket_tts.tts import PocketTTSService

            return PocketTTSService(settings=PocketTTSService.Settings(voice=voice))
        case "deepgram":
            from pipecat.services.deepgram.tts import DeepgramTTSService

            settings = DeepgramTTSService.Settings(**_given({"model": model, "voice": voice}))
            return DeepgramTTSService(api_key=key, settings=settings)
        case "openai":
            from pipecat.services.openai.tts import OpenAITTSService

            settings = OpenAITTSService.Settings(
                **_given({"model": model, "voice": voice}), speed=speed
            )
            return OpenAITTSService(api_key=key, settings=settings)
        case _:
            return _server_tts(service, speed=speed)


async def window(service: Service) -> int | None:
    """Gives the context size the operator's server reports for the model, or None."""
    if service.provider not in SERVERS:
        return None
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            reply = await client.get(f"{service.url.rstrip('/')}/models", headers=_auth(service))
        rows = reply.json().get("data", [])
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    for row in rows:
        if isinstance(row, dict) and row.get("id") == service.model:
            size = row.get("max_model_len") or row.get("context_length")  # vLLM, OpenRouter shape
            size = size or (row.get("meta") or {}).get("n_ctx_train")  # llama.cpp
            return int(size) if size else None
    return None


def context(service: Service, *, window: int | None = None) -> LLMAssistantAggregatorParams:
    """Summarizes the older messages once the context passes CONTEXT_TOKENS, or most of a
    smaller server window. A size trigger only, so the cached prefix holds between summaries."""
    summary = LLMContextSummaryConfig(
        target_context_tokens=SUMMARY_TOKENS,
        min_messages_after_summary=KEPT_MESSAGES,
        summarization_prompt=SUMMARY_PROMPT,
        summary_message_template="Summary of the voice session so far:\n{summary}",
        llm=llm(service, prompt="", max_tokens=SUMMARY_TOKENS),
    )
    return LLMAssistantAggregatorParams(
        enable_auto_context_summarization=True,
        auto_context_summarization_config=LLMAutoContextSummarizationConfig(
            max_context_tokens=min(CONTEXT_TOKENS, int((window or CONTEXT_TOKENS) * WINDOW_SHARE)),
            max_unsummarized_messages=None,
            summary_config=summary,
        ),
    )


def turn() -> LLMUserAggregatorParams:
    """Silero finds speech, and smart-turn decides on the device when the operator is done."""
    stop = TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())
    return LLMUserAggregatorParams(
        vad_analyzer=SileroVADAnalyzer(),
        user_turn_strategies=UserTurnStrategies(stop=[stop]),
        empty_user_turn=EmptyUserTurnConfig(interrupted_prompt=SPOKE_OVER),
    )


def _compatible(service: Service, common: dict[str, Any]) -> LLMService[Any]:
    """Any server on the OpenAI chat shape: groq, ollama, llama.cpp, vLLM and others. The
    developer role goes as the user role, since many such servers refuse it."""
    from pipecat.services.openai.llm import OpenAILLMService

    class Compatible(OpenAILLMService):
        supports_developer_role = False

    url = service.url or (GROQ_URL if service.provider == "groq" else "")
    if not url:
        raise ValueError("VOICE_RELAY_LLM_URL: expected the base url of the llm server, got none")
    key = service.key or "none"
    settings = OpenAILLMService.Settings(**_given(common), extra=_body(service.body))
    return Compatible(api_key=key, base_url=url, settings=settings)


def _cached(service: type[Any]) -> type[Any]:
    """Marks cache points for Claude models on OpenRouter: the system prompt and the 2 newest
    text messages. Anthropic caches only marked prefixes, other providers cache by themselves."""

    class Cached(service):  # type: ignore[valid-type, misc]
        def build_chat_completion_params(self, params_from_context: Any) -> dict[str, Any]:
            params = super().build_chat_completion_params(params_from_context)
            if str(params.get("model", "")).startswith("anthropic/"):
                params["messages"] = _marked(list(params["messages"]))
            return params

    return Cached


def _marked(messages: list[Any]) -> list[Any]:
    texts = [
        index
        for index, message in enumerate(messages)
        if message.get("role") != "tool" and isinstance(message.get("content"), str)
    ]
    points = {index for index in texts if messages[index].get("role") == "system"} | set(texts[-2:])
    for index in points:
        text = messages[index]["content"]
        block = {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}
        messages[index] = {**messages[index], "content": [block]}
    return messages


def _server_tts(service: Service, *, speed: float) -> TTSService:
    """The OpenAI speech API on the operator's server, with any voice that server has: the
    stock service accepts only OpenAI's voice names."""
    from openai import APIError
    from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame
    from pipecat.services.openai.tts import OpenAITTSService

    class ServerTTS(OpenAITTSService):
        async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
            params = {"input": text, "model": service.model, "voice": service.voice}
            speech = self._client.audio.speech.with_streaming_response
            try:
                async with speech.create(**params, response_format="pcm", speed=speed) as reply:
                    await self.start_tts_usage_metrics(text)
                    async for chunk in reply.iter_bytes(self.chunk_size):
                        await self.stop_ttfb_metrics()
                        yield TTSAudioRawFrame(chunk, self.sample_rate, 1, context_id=context_id)
            except (APIError, httpx.HTTPError) as error:
                yield ErrorFrame(error=f"your speech server: {type(error).__name__}: {error}")

    return ServerTTS(
        api_key=service.key or "none",
        base_url=service.url,
        sample_rate=24_000,  # the PCM rate of the OpenAI speech API
        settings=OpenAITTSService.Settings(model=service.model, voice=service.voice),
    )


def _whisper_compute() -> str:
    """int8 on a CPU, where the stored float16 weights run slowly; float16 on a GPU."""
    import ctranslate2

    return "float16" if ctranslate2.get_cuda_device_count() else "int8"


def _auth(service: Service) -> dict[str, str]:
    return {"authorization": f"Bearer {service.key}"} if service.key else {}


def _given(fields: dict[str, Any]) -> dict[str, Any]:
    return {name: value for name, value in fields.items() if value}


def _body(text: str, defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    body = {**(defaults or {}), **(json.loads(text) if text else {})}
    return {"extra_body": body} if body else {}
