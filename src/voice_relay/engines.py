"""Engines that run on this computer. Each has packages, installed on request from its extra
in pyproject.toml, and a model or a voice it downloads. Both sizes are shown before the
operator agrees; a model can be removed again."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from platformdirs import user_cache_dir

ROOT = Path(__file__).resolve().parents[2]  # the project uv installs the extras into
CACHE = Path.home() / ".cache"
KOKORO_DIR = CACHE / "pipecat" / "kokoro-onnx"  # where pipecat looks
PIPER_DIR = CACHE / "pipecat" / "piper"
KOKORO_FILES = {
    "kokoro-v1.0.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
    "voices-v1.0.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
}  # fmt: skip
PIPER_VOICES = "https://huggingface.co/rhasspy/piper-voices/resolve/main/voices.json"
MOONSHINE_ARCH = "small-streaming"  # pipecat's default
WHISPER_MB = {
    "tiny": 75,
    "base": 145,
    "small": 485,
    "medium": 1530,
    "large-v3-turbo": 1620,
    "large-v3": 3090,
}
POCKET_VOICES = ("alba", "marius", "javert", "jean", "anna", "vera", "fantine", "charles", "paul",
                 "eponine", "azelma", "george", "mary", "jane", "michael", "eve")  # fmt: skip
POCKET_MB = 450
DEFAULTS = {
    "whisper": "small",
    "piper": "en_US-lessac-medium",
    "pocket": "alba",
    "kokoro": "af_heart",
}
CHOSEN_BY = {
    "whisper": "VOICE_RELAY_STT_MODEL",
    "piper": "VOICE_RELAY_TTS_VOICE",
    "pocket": "VOICE_RELAY_TTS_VOICE",
}
LOW_MEMORY_GB = 8
LOW_CORES = 4
CHUNK = 1 << 20

Progress = Callable[[float], None]


@dataclass(frozen=True)
class Engine:
    provider: str
    stage: str
    extra: str
    module: str  # importable once the extra is installed
    package_mb: int


ENGINES = {
    engine.provider: engine
    for engine in (
        Engine("moonshine", "stt", "moonshine", "moonshine_voice", 50),
        Engine("whisper", "stt", "whisper", "faster_whisper", 15),
        Engine("kokoro", "tts", "kokoro", "kokoro_onnx", 25),
        Engine("piper", "tts", "piper", "piper", 50),
        Engine("pocket", "tts", "pocket", "pocket_tts", 600),
    )
}


def variant(provider: str, env: Mapping[str, str]) -> str:
    """The model or voice of the engine that the settings ask for, or its default."""
    if provider == "moonshine":
        return _base(env.get("VOICE_RELAY_STT_LANGUAGE", ""))
    field = CHOSEN_BY.get(provider, "")
    stage = field.split("_")[2] if field else ""
    own = env.get(f"VOICE_RELAY_{stage}") == provider  # a voice of another provider
    return (own and env.get(field)) or DEFAULTS[provider]


def ready(provider: str, chosen: str) -> bool:
    return _packages(provider) and _model_here(provider, chosen)


def need_mb(provider: str, chosen: str) -> int:
    """What a first use downloads: the packages, the model, or both."""
    packages = 0 if _packages(provider) else ENGINES[provider].package_mb
    model = (
        0 if _packages(provider) and _model_here(provider, chosen) else _model_mb(provider, chosen)
    )
    return packages + model


def remove(provider: str) -> None:
    """Deletes the engine's models, then its packages. A package another installed engine
    needs stays: uv keeps exactly the extras of the engines left."""
    kept = [name for name in ENGINES if name != provider and _packages(name)]
    _models(provider)
    if _packages(provider):
        _sync(*(flag for name in kept for flag in ("--extra", ENGINES[name].extra)))


def _models(provider: str) -> None:
    hub = Path(os.environ.get("HF_HUB_CACHE", CACHE / "huggingface" / "hub"))
    folders = {
        "kokoro": [KOKORO_DIR],
        "moonshine": [_moonshine_dir()],
        "piper": [PIPER_DIR],
        "whisper": [*hub.glob("models--*faster-whisper*")],
        "pocket": [*hub.glob("models--kyutai--*")],
    }[provider]
    for folder in folders:
        shutil.rmtree(folder, ignore_errors=True)
    for marker in _marker(provider, "").parent.glob(f"{provider}-*.ready"):
        marker.unlink()


def piper_voices() -> list[dict[str, Any]]:
    """Piper's catalog: id, language and size of each voice."""
    rows = sorted(_piper_catalog().items(), key=lambda item: (_language(item[1]), item[0]))
    return [
        {"id": name, "name": f"{_language(row)} · {row['name']} · {row['quality']}",
         "detail": f"{row['language']['code']}, about {_piper_mb(row)} MB"}
        for name, row in rows
    ]  # fmt: skip


def machine() -> dict[str, Any]:
    """Memory and cores, and a warning when a local engine can lag here."""
    memory, cores = _memory_gb(), os.cpu_count() or 1
    weak = memory < LOW_MEMORY_GB or cores < LOW_CORES
    warning = (
        f"This computer has {memory:.0f} GB of memory and {cores} cores. A local engine can lag "
        "here; the API providers are faster."
    )
    return {"memory_gb": round(memory), "cores": cores, "warning": warning if weak else ""}


class Installs:
    """One install thread per engine at most; the state for the pages to poll."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def start(self, provider: str, chosen: str) -> None:
        with self._lock:
            if self.state(provider, chosen)["state"] in ("installing", "downloading", "ready"):
                return
            self._jobs[provider] = {"state": "installing", "progress": 0.0, "error": ""}
        threading.Thread(target=self._run, args=(provider, chosen), daemon=True).start()

    def state(self, provider: str, chosen: str) -> dict[str, Any]:
        job = self._jobs.get(provider)
        if job is not None and job["state"] != "ready":
            return job
        here = ready(provider, chosen)
        return {"state": "ready" if here else "missing", "progress": 0.0, "error": ""}

    def busy(self) -> bool:
        return any(job["state"] in ("installing", "downloading") for job in self._jobs.values())

    def _run(self, provider: str, chosen: str) -> None:
        job = self._jobs[provider]
        try:
            if not _packages(provider):  # --inexact: the other engines stay
                _sync("--inexact", "--extra", ENGINES[provider].extra)
            job.update(state="downloading")
            FETCH[provider](chosen, lambda fraction: job.update(progress=round(fraction, 3)))
            job.update(state="ready", progress=1.0)
        except Exception as error:  # a failed install must reach the page, whatever failed
            job.update(state="failed", error=f"{type(error).__name__}: {error}"[:300])


def _sync(*flags: str) -> None:
    """Runs uv sync on the plugin's own environment with the given extras."""
    uv = os.environ.get("UV") or shutil.which("uv")
    if uv is None or not (ROOT / "pyproject.toml").exists():
        raise RuntimeError("uv sync: expected uv and the plugin's pyproject.toml, found none")
    command = [uv, "sync", "--frozen", "--no-dev", *flags, "--project", str(ROOT)]
    done = subprocess.run(command, capture_output=True, text=True, timeout=1800)
    if done.returncode != 0:
        raise RuntimeError(
            done.stderr.strip().splitlines()[-1] if done.stderr.strip() else "uv sync"
        )
    importlib.invalidate_caches()


def _packages(provider: str) -> bool:
    return importlib.util.find_spec(ENGINES[provider].module) is not None


def _model_here(provider: str, chosen: str) -> bool:
    if provider == "kokoro":
        return all((KOKORO_DIR / name).is_file() for name in KOKORO_FILES)
    if provider == "moonshine":
        found = _moonshine_dir().rglob(f"*-{chosen}")
        return any(not folder.name.startswith("spelling") for folder in found)
    if provider == "piper":
        return (PIPER_DIR / f"{chosen}.onnx").is_file() and (
            PIPER_DIR / f"{chosen}.onnx.json"
        ).is_file()
    if provider == "whisper":
        from faster_whisper.utils import download_model

        try:
            download_model(chosen, local_files_only=True)
        except Exception:  # not in the cache, in whatever way the hub reports it
            return False
        return True
    return _marker(provider, chosen).exists()


def _model_mb(provider: str, chosen: str) -> int:
    if provider == "whisper":
        return WHISPER_MB.get(chosen, WHISPER_MB["large-v3"])
    if provider == "piper":
        try:
            row = _piper_catalog().get(chosen)
        except (httpx.HTTPError, ValueError):  # offline: the size of a medium voice
            row = None
        return _piper_mb(row) if row else 60
    return {"kokoro": 340, "moonshine": 140, "pocket": POCKET_MB}[provider]


def _fetch_kokoro(chosen: str, progress: Progress) -> None:
    KOKORO_DIR.mkdir(parents=True, exist_ok=True)
    total, done = 340 << 20, 0
    for name, url in KOKORO_FILES.items():
        target = KOKORO_DIR / name
        if target.is_file():
            continue
        partial = target.with_suffix(".part")  # a cut download never looks complete
        with httpx.stream("GET", url, follow_redirects=True, timeout=60) as reply:
            reply.raise_for_status()
            with partial.open("wb") as file:
                for chunk in reply.iter_bytes(CHUNK):
                    file.write(chunk)
                    done += len(chunk)
                    progress(min(done / total, 0.99))
        partial.replace(target)


def _fetch_moonshine(chosen: str, progress: Progress) -> None:
    """The model pipecat loads for the language: small-streaming, or the best one published."""
    from moonshine_voice import get_model_for_language, string_to_model_arch

    report = {"on_progress": lambda fraction, *_: progress(fraction)}
    try:
        get_model_for_language(chosen, string_to_model_arch(MOONSHINE_ARCH), **report)
    except ValueError:
        get_model_for_language(chosen, **report)


def _fetch_whisper(chosen: str, progress: Progress) -> None:
    from faster_whisper.utils import download_model

    hub = Path(os.environ.get("HF_HUB_CACHE", CACHE / "huggingface" / "hub"))
    _watched(
        lambda: download_model(chosen),
        hub,
        "models--*faster-whisper*",
        _model_mb("whisper", chosen),
        progress,
    )


def _fetch_piper(chosen: str, progress: Progress) -> None:
    from piper.download_voices import download_voice

    PIPER_DIR.mkdir(parents=True, exist_ok=True)
    _watched(
        lambda: download_voice(chosen, PIPER_DIR),
        PIPER_DIR,
        f"{chosen}*",
        _model_mb("piper", chosen),
        progress,
    )


def _fetch_pocket(chosen: str, progress: Progress) -> None:
    """Loads the English model and the voice once, which downloads both into the hub cache."""

    def load() -> None:
        from pocket_tts import TTSModel

        model = TTSModel.load_model(language="english")
        model.get_state_for_audio_prompt(chosen)

    hub = Path(os.environ.get("HF_HUB_CACHE", CACHE / "huggingface" / "hub"))
    _watched(load, hub, "models--kyutai--*", POCKET_MB, progress)
    _marker("pocket", chosen).parent.mkdir(parents=True, exist_ok=True)
    _marker("pocket", chosen).touch()


def _watched(
    fetch: Callable[[], Any], folder: Path, pattern: str, megabytes: int, progress: Progress
) -> None:
    """Runs a download that reports nothing, and reports the growth of what it writes."""
    errors: list[BaseException] = []

    def run() -> None:
        try:
            fetch()
        except BaseException as error:  # handed to the caller's thread below
            errors.append(error)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    while worker.is_alive():
        size = sum(
            p.stat().st_size
            for match in folder.glob(pattern)
            for p in ([match] if match.is_file() else match.rglob("*"))
            if p.is_file()
        )
        progress(min(size / (megabytes << 20), 0.99))
        time.sleep(0.5)
    if errors:
        raise errors[0]


def _piper_catalog() -> dict[str, Any]:
    cached = CACHE / "pipecat" / "piper-voices.json"
    if not cached.exists() or time.time() - cached.stat().st_mtime > 7 * 86400:
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(httpx.get(PIPER_VOICES, follow_redirects=True, timeout=30).content)
    return json.loads(cached.read_text(encoding="utf-8"))


def _language(row: Mapping[str, Any]) -> str:
    return str(row["language"]["name_english"])


def _piper_mb(row: Mapping[str, Any]) -> int:
    return max(1, round(sum(file["size_bytes"] for file in row["files"].values()) / 2**20))


def _marker(provider: str, chosen: str) -> Path:
    return CACHE / "pipecat" / "voice-relay" / f"{provider}-{chosen}.ready"


def _base(language: str) -> str:
    code = language.split("-")[0].lower()
    return code if code and code != "multi" else "en"


def _moonshine_dir() -> Path:
    return Path(os.environ.get("MOONSHINE_VOICE_CACHE", user_cache_dir("moonshine_voice")))


def _memory_gb() -> float:
    if sys.platform == "win32":
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong)
                for name in ("total", "free", "page", "page_free", "virtual", "virtual_free", "x")
            ]

        status = Status(length=ctypes.sizeof(Status))
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))  # type: ignore[attr-defined]
        return status.total / 2**30
    return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30


FETCH: dict[str, Callable[[str, Progress], None]] = {
    "kokoro": _fetch_kokoro,
    "moonshine": _fetch_moonshine,
    "whisper": _fetch_whisper,
    "piper": _fetch_piper,
    "pocket": _fetch_pocket,
}
