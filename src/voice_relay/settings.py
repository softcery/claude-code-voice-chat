"""Settings: the config file under the process environment, read again for each voice session."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, set_key, unset_key

from . import engines

CONFIG_DIR = Path(os.environ.get("VOICE_RELAY_HOME", Path.home() / ".config" / "voice-relay"))
CONFIG_FILE = CONFIG_DIR / ".env"
TOKEN_FILE = CONFIG_DIR / "token"  # the mod reads it too
PROMPTS = Path(__file__).parent / "prompts"
PROMPT_FILE = CONFIG_DIR / "prompt.md"
RULES_FILE = CONFIG_DIR / "rules.md"
BASED_ON = "<!-- voice-relay: a copy of the built-in prompt {digest} -->\n"
ENCODING = "utf-8"
HOST = "127.0.0.1"  # the server never listens beyond this computer
TOKEN_BYTES = 16
PROCESS = dict(os.environ)  # wins over the config file

SERVER = "compatible"  # the operator's own server on the OpenAI API, for each stage
APPS = {  # LLM apps the operator installs and runs on this computer, at their default port
    "ollama": "http://localhost:11434/v1",
    "lmstudio": "http://localhost:1234/v1",
    "llamacpp": "http://localhost:8080/v1",
}
# The first provider of each part is its default.
STT = ("elevenlabs", "deepgram", "assemblyai", "cartesia", "openai", "moonshine", "whisper", SERVER)
LLM = ("openrouter", "openai", "anthropic", "google", "groq", *APPS, SERVER)
TTS = (
    "elevenlabs",
    "cartesia",
    "openai",
    "deepgram",
    "kokoro",
    "piper",
    "pocket",
    SERVER,
)
MODELS = {  # fast, cheap, and right on the agent's test commands
    "openrouter": "anthropic/claude-haiku-5.5",
    "anthropic": "claude-haiku-5-5",
    "openai": "gpt-6-luna",
    "google": "gemini-3.6-flash",
    "groq": "openai/gpt-oss-120b",
}
VOICES = {
    "cartesia": "5abd2130-146a-41b1-bcdb-974ea8e19f56",
    "elevenlabs": "21m00Tcm4TlvDq8ikWAM",
    **{name: engines.DEFAULTS[name] for name in ("kokoro", "piper", "pocket")},
}
KEYS = {
    "assemblyai": "ASSEMBLYAI_API_KEY",
    "deepgram": "DEEPGRAM_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "cartesia": "CARTESIA_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "groq": "GROQ_API_KEY",
}
FIELDS = (
    "VOICE_RELAY_STT",
    "VOICE_RELAY_STT_MODEL",
    "VOICE_RELAY_STT_LANGUAGE",
    "VOICE_RELAY_STT_URL",
    "VOICE_RELAY_LLM",
    "VOICE_RELAY_LLM_MODEL",
    "VOICE_RELAY_LLM_URL",
    "VOICE_RELAY_LLM_BODY",
    "VOICE_RELAY_TTS",
    "VOICE_RELAY_TTS_MODEL",
    "VOICE_RELAY_TTS_VOICE",
    "VOICE_RELAY_TTS_URL",
    "VOICE_RELAY_TTS_SPEED",
    "VOICE_RELAY_PROJECTS",
    "VOICE_RELAY_TERMINAL",
    "VOICE_RELAY_NARRATION_SECONDS",
)
SECRETS = (*KEYS.values(), "VOICE_RELAY_STT_KEY", "VOICE_RELAY_LLM_KEY", "VOICE_RELAY_TTS_KEY")
SERVERS = (SERVER, *APPS)  # their key comes from VOICE_RELAY_<STAGE>_KEY
TEXTS = ("rules", "prompt")  # Markdown files, not .env values


class SettingsError(ValueError):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class Service:
    """One speech or language service. An empty model, language or voice keeps the default."""

    provider: str
    model: str = ""
    language: str = ""
    voice: str = ""
    url: str = ""
    key: str = ""
    body: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class Settings:
    stt: Service
    llm: Service
    tts: Service
    prompt: str
    greeting: str
    narration_seconds: float
    speed: float


def environment() -> dict[str, str]:
    """Gives the config file values under the process environment."""
    values = {name: value or "" for name, value in dotenv_values(CONFIG_FILE).items()}
    return {**values, **PROCESS}


def load(env: Mapping[str, str] | None = None) -> Settings:
    """Reads all settings of one session. A value of the wrong shape raises SettingsError."""
    env = environment() if env is None else env
    return Settings(
        stt=_service(env, "STT", STT),
        llm=_service(env, "LLM", LLM),
        tts=_service(env, "TTS", TTS),
        prompt=prompt(),
        greeting=(PROMPTS / "greeting.md").read_text(encoding=ENCODING),
        narration_seconds=_number(env, "VOICE_RELAY_NARRATION_SECONDS", 30.0, 10.0, 300.0),
        speed=_number(env, "VOICE_RELAY_TTS_SPEED", 1.0, 0.7, 1.2),  # the range of each provider
    )


def problems(env: Mapping[str, str] | None = None) -> list[str]:
    """Names what stops a session: a wrong value or a missing key of a chosen provider."""
    env = environment() if env is None else env
    try:
        settings = load(env)
    except SettingsError as error:
        return [str(error)]
    found: list[str] = []
    for stage, service in (("STT", settings.stt), ("LLM", settings.llm), ("TTS", settings.tts)):
        name = f"VOICE_RELAY_{stage}"
        if service.provider in KEYS and not service.key:
            found.append(f"{KEYS[service.provider]}: expected an API key for {service.provider}")
        engine = service.provider in engines.ENGINES
        if engine and not engines.ready(service.provider, engines.variant(service.provider, env)):
            found.append(f"{name}: expected the {service.provider} model on this computer")
        if service.provider == SERVER and not service.url:
            found.append(f"{name}_URL: expected the url of your server")
        if service.provider in SERVERS and not service.model:
            found.append(f"{name}_MODEL: expected the model of your server")
            if stage == "TTS" and not service.voice:
                found.append(f"{name}_VOICE: expected a voice your server has")
    return sorted(set(found))


def save(values: Mapping[str, str]) -> None:
    """Writes the given settings and keys into the config file, and the rules and the prompt
    into their files. An empty value removes one."""
    for name in values:
        if name not in FIELDS and name not in SECRETS and name not in TEXTS:
            raise SettingsError(f"{name}: expected a known setting name, got another name")
    env = {name: value.strip() for name, value in values.items() if name not in TEXTS}
    load({**environment(), **env, **PROCESS})
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.touch(mode=0o600)
    if "rules" in values:
        _write(RULES_FILE, values["rules"].strip())
    if "prompt" in values:
        custom = values["prompt"].strip()
        same = custom == builtin().strip()
        _write(
            PROMPT_FILE, "" if same or not custom else BASED_ON.format(digest=_digest()) + custom
        )
    for name, value in env.items():
        if value:
            set_key(CONFIG_FILE, name, value.strip(), quote_mode="auto")
        elif name in dotenv_values(CONFIG_FILE):
            unset_key(CONFIG_FILE, name)


def port() -> int:
    return _number(environment(), "VOICE_RELAY_PORT", 7860, 1024, 65535)


def prompt() -> str:
    """Gives the system prompt: the operator's copy or the built-in one, their rules appended."""
    own = texts()
    text, rules = own["prompt"] or builtin(), own["rules"]
    return f"{text}\n<operator_rules>\n{rules}\n</operator_rules>\n" if rules else text


def builtin() -> str:
    return (PROMPTS / "system.md").read_text(encoding=ENCODING)


def texts() -> dict[str, str]:
    rules = RULES_FILE.read_text(encoding=ENCODING).strip() if RULES_FILE.exists() else ""
    custom = PROMPT_FILE.read_text(encoding=ENCODING) if PROMPT_FILE.exists() else ""
    first, _, rest = custom.partition("\n")
    return {"rules": rules, "prompt": (rest if first.startswith("<!--") else custom).strip()}


def outdated() -> bool:
    """True when the operator's prompt copy was made from an older built-in prompt."""
    if not PROMPT_FILE.exists():
        return False
    first = PROMPT_FILE.read_text(encoding=ENCODING).partition("\n")[0]
    return first.startswith("<!--") and _digest() not in first


def token() -> str:
    """Reads the bridge token, written at mode 600 on first use."""
    if not TOKEN_FILE.exists():
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.touch(mode=0o600)
        TOKEN_FILE.write_text(secrets.token_hex(TOKEN_BYTES), encoding=ENCODING)
    return TOKEN_FILE.read_text(encoding=ENCODING).strip()


def _write(path: Path, text: str) -> None:
    if text:
        path.write_text(f"{text}\n", encoding=ENCODING)
    else:
        path.unlink(missing_ok=True)


def _digest() -> str:
    return hashlib.sha256(builtin().encode(ENCODING)).hexdigest()[:12]


def _text(env: Mapping[str, str], name: str) -> str:
    return (env.get(name) or "").strip()


def _service(env: Mapping[str, str], stage: str, options: tuple[str, ...]) -> Service:
    """Reads one stage the same way for STT, LLM and TTS: VOICE_RELAY_<STAGE>_*."""
    name = f"VOICE_RELAY_{stage}"
    provider = _choice(env, name, options)
    return Service(
        provider=provider,
        model=_text(env, f"{name}_MODEL"),
        language=_text(env, f"{name}_LANGUAGE"),
        voice=_text(env, f"{name}_VOICE"),
        url=APPS.get(provider) or (_text(env, f"{name}_URL") if provider == SERVER else ""),
        key=_text(env, f"{name}_KEY") if provider in SERVERS else _key(env, provider),
        body=_body(env) if stage == "LLM" else "",
    )


def _key(env: Mapping[str, str], provider: str) -> str:
    return _text(env, KEYS[provider]) if provider in KEYS else ""


def _choice(env: Mapping[str, str], name: str, options: tuple[str, ...]) -> str:
    value = _text(env, name).lower() or options[0]
    if value not in options:
        raise SettingsError(f"{name}: expected one of {', '.join(options)}, got another value")
    return value


def _number[N: (int, float)](env: Mapping[str, str], name: str, default: N, low: N, high: N) -> N:
    value = _text(env, name)
    if not value:
        return default
    try:
        number = type(default)(value)
    except ValueError:
        raise SettingsError(
            f"{name}: expected a {type(default).__name__}, got a text of {len(value)} characters"
        ) from None
    if not low <= number <= high:
        raise SettingsError(f"{name}: expected a number from {low} to {high}, got one outside")
    return number


def _body(env: Mapping[str, str]) -> str:
    text = _text(env, "VOICE_RELAY_LLM_BODY")
    if not text:
        return ""
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        raise SettingsError(
            "VOICE_RELAY_LLM_BODY: expected a JSON object, got invalid JSON"
        ) from None
    if not isinstance(body, dict):
        raise SettingsError(
            f"VOICE_RELAY_LLM_BODY: expected a JSON object, got {type(body).__name__}"
        )
    return text
