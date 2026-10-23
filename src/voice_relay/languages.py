"""The languages each speech recognition provider takes, from the source of truth: pipecat's
own maps of verified languages, Deepgram's model list, and Moonshine's model catalog."""

from __future__ import annotations

import ast
import importlib
import inspect
import textwrap
from functools import cache

import httpx

DEEPGRAM_MODELS = "https://api.deepgram.com/v1/models"
DEEPGRAM_MODEL = "nova-3-general"  # the model pipecat streams by default
WHISPER = ("pipecat.services.whisper.base_stt", "language_to_whisper_language")
PIPECAT_MAPS = {
    "elevenlabs": ("pipecat.services.elevenlabs.stt", "language_to_elevenlabs_language"),
    "assemblyai": ("pipecat.services.assemblyai.stt", "language_to_assemblyai_language"),
    "openai": WHISPER,
    "cartesia": WHISPER,  # ink-whisper
    "compatible": WHISPER,  # the OpenAI transcription API
    "whisper": WHISPER,
}
MOONSHINE = (
    "ar",
    "de",
    "en",
    "es",
    "ja",
    "ko",
    "tl",
    "uk",
    "vi",
    "zh",
)  # its own list, before the package is in


async def languages(provider: str, *, model: str, key: str) -> dict[str, object]:
    """Gives {codes} for the provider, with an error when its list is out of reach."""
    if provider in PIPECAT_MAPS:
        return {"codes": _pipecat(*PIPECAT_MAPS[provider])}
    if provider == "moonshine":
        try:
            from moonshine_voice import supported_languages
        except ImportError:
            return {"codes": list(MOONSHINE)}
        return {"codes": sorted(supported_languages())}
    if provider == "deepgram":
        return await _deepgram(model or DEEPGRAM_MODEL, key)
    return {"codes": []}


@cache
def _pipecat(module: str, function: str) -> list[str]:
    """Reads the keys of LANGUAGE_MAP inside a pipecat function: the languages it verified."""
    source = textwrap.dedent(inspect.getsource(getattr(importlib.import_module(module), function)))
    language = importlib.import_module("pipecat.transcriptions.language").Language
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            names = [key.attr for key in node.value.keys if isinstance(key, ast.Attribute)]
            return sorted({language[name].value.split("-")[0] for name in names})
    return []


async def _deepgram(model: str, key: str) -> dict[str, object]:
    if not key:
        return {"codes": [], "error": "Add the Deepgram key to list its languages."}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            reply = await client.get(DEEPGRAM_MODELS, headers={"authorization": f"Token {key}"})
            reply.raise_for_status()
        rows = reply.json().get("stt", [])
    except (httpx.HTTPError, ValueError) as error:
        return {
            "codes": [],
            "error": f"Deepgram did not list its languages: {type(error).__name__}",
        }
    wanted = model if model.count("-") > 1 else f"{model}-general"  # nova-3 is nova-3-general
    codes = {code for row in rows if row.get("canonical_name") == wanted and row.get("streaming")
             for code in row.get("languages", [])}  # fmt: skip
    return {"codes": sorted({code.split("-")[0] for code in codes} | {"multi"})}
