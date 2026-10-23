"""The Test button: each stage gets real input through its own pipecat service, the same one a
voice session uses, and gives a verdict with numbers in place of a hardware guess."""

from __future__ import annotations

import asyncio
import base64
import io
import re
import time
import wave
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    FunctionCallsStartedFrame,
    InputAudioRawFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStoppedFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner

from . import services
from .bridge import DEVELOPER, LINKED, PROGRESS, TOOLS
from .settings import SERVER, Settings

SAMPLE = Path(__file__).parent / "sample.wav"
SAMPLE_TEXT = "Run the tests in the shop project, but do not fix anything."
SPOKEN = "Hello, I am your voice agent. Tell me what Claude Code should do."
SECONDS = 20.0
STT_WAIT = 8.0
ENGLISH = ("", "en", "en-us", "en-gb", "multi")
CHUNK_SECONDS = 0.1
HEARD_SHARE = 0.6  # of the sample's words, so one misheard word still passes
SLOW_SECONDS = 2.0
NO_AUDIO = "the voice gave no audio: check the key, the model and the voice name"
SMALL_WINDOW = 16_000


def _said(text: str) -> dict[str, str]:
    return {"role": "user", "content": text}


LINKED_NOW = {"role": DEVELOPER, "content": LINKED.format(names="shop")}
PROGRESS_NOW = {"role": DEVELOPER, "content": PROGRESS.format(progress="shop: edits billing.py")}
COMMANDS: list[tuple[str, list[dict[str, Any]], Callable[[str, str, str], bool]]] = [
    (
        "send a task word for word",
        [LINKED_NOW, _said("Tell Claude Code to run the tests, but don't fix anything.")],
        lambda tool, args, text: tool == "send_task" and "fix" in args.lower(),
    ),
    (
        "stop the running turn",
        [LINKED_NOW, _said("Ask Claude Code to refactor the billing module."),
         {"role": "assistant", "content": "Sent."}, PROGRESS_NOW, _said("Stop it, stop it now.")],
        lambda tool, args, text: tool == "abort",
    ),
    (
        "read a push back before sending it",
        [LINKED_NOW, _said("Tell Claude Code to force push to main.")],
        lambda tool, args, text: tool is None and bool(text.strip()),
    ),
]  # fmt: skip


async def check(stage: str, current: Settings) -> dict[str, Any]:
    """Gives {ok, summary} for the stage, and for tts the spoken audio as a WAV data URL."""
    try:
        if stage == "llm":
            return await _llm(current)
        if stage == "stt":
            return await _stt(current)
        return await _tts(current)
    except Exception as error:  # the verdict must reach the page, whatever the service raised
        return {"ok": False, "summary": f"{type(error).__name__}: {error}"[:300]}


async def _llm(current: Settings) -> dict[str, Any]:
    async def one(messages: list[dict[str, Any]]) -> tuple[str | None, str, str, float, str]:
        llm = services.llm(current.llm, prompt=current.prompt)
        context = LLMContext(messages=messages, tools=ToolsSchema(standard_tools=list(TOOLS)))  # type: ignore[arg-type]
        frames, error = await _run(
            llm, [LLMContextFrame(context=context)], _ends(LLMFullResponseEndFrame)
        )
        calls = [f for _, f in frames if isinstance(f, FunctionCallsStartedFrame)]
        call = calls[0].function_calls[0] if calls else None
        text = "".join(f.text for _, f in frames if isinstance(f, LLMTextFrame))
        answered = [
            t for t, f in frames if isinstance(f, (LLMTextFrame, FunctionCallsStartedFrame))
        ]
        return (call.function_name if call else None, str(call.arguments) if call else "", text,
                answered[0] if answered else SECONDS, error)  # fmt: skip

    results = await asyncio.gather(*(one(messages) for _, messages, _ in COMMANDS))
    errors = [error for *_, error in results if error]
    if errors:
        return {"ok": False, "summary": errors[0]}
    passed = [
        name
        for (name, _, test), (tool, args, text, _, _) in zip(COMMANDS, results, strict=True)
        if test(tool, args, text)
    ]
    failed = [name for name, _, _ in COMMANDS if name not in passed]
    first = sorted(seconds for *_, seconds, _ in results)[len(results) // 2]
    window = await services.window(current.llm) if current.llm.provider == SERVER else None
    notes = [f"answers in {first:.1f} s", f"{len(passed)} of {len(COMMANDS)} commands right"]
    if failed:
        notes.append(f"missed: {', '.join(failed)}")
    if first > SLOW_SECONDS:
        notes.append("too slow for a spoken reply")
    if window and window < SMALL_WINDOW:
        notes.append(
            f"context window {window} tokens is too small, give the server {SMALL_WINDOW}+"
        )
    ok = not failed and first <= SLOW_SECONDS and not (window and window < SMALL_WINDOW)
    return {"ok": ok, "summary": " · ".join(notes)}


async def _stt(current: Settings) -> dict[str, Any]:
    with wave.open(str(SAMPLE)) as sample:
        rate, pcm = sample.getframerate(), sample.readframes(sample.getnframes())
    step = int(rate * CHUNK_SECONDS) * 2
    silence = bytes(step)
    audio: list[Frame | float] = [VADUserStartedSpeakingFrame()]
    for start in range(0, len(pcm), step):
        audio += [InputAudioRawFrame(pcm[start : start + step], rate, 1), CHUNK_SECONDS]
    audio += [InputAudioRawFrame(silence, rate, 1), CHUNK_SECONDS] * 5
    audio += [VADUserStoppedSpeakingFrame(), UserStoppedSpeakingFrame()]
    stt = services.stt(current.stt)
    frames, error = await _run(stt, audio, _ends(TranscriptionFrame), rate=rate, seconds=STT_WAIT)
    english = current.stt.language.lower() in ENGLISH
    if english and error:
        return {"ok": False, "summary": error}
    texts = [(t, f.text) for t, f in frames if isinstance(f, TranscriptionFrame)]
    heard = " ".join(text for _, text in texts).strip()
    if not english:  # the recording is English: it shows the link works, not the accuracy
        broken = error if error and not error.startswith("no answer") else ""
        language = current.stt.language
        return {"ok": not broken, "summary": broken or f"connected · the test speaks English, so "
                f"it does not check {language} recognition"}  # fmt: skip
    if not heard:
        return {"ok": False, "summary": "heard nothing in the test recording"}
    words = set(_words(SAMPLE_TEXT))
    share = len(words & set(_words(heard))) / len(words)
    after = texts[0][0] - (len(pcm) / 2 / rate + 5 * CHUNK_SECONDS)
    summary = f'heard "{heard}" · {max(after, 0):.1f} s after the speech ended'
    return {
        "ok": share >= HEARD_SHARE,
        "summary": summary if share >= HEARD_SHARE else f"misheard: {summary}",
    }


async def _tts(current: Settings) -> dict[str, Any]:
    tts = services.tts(current.tts, speed=current.speed)
    frames, error = await _run(tts, [TTSSpeakFrame(SPOKEN)], _ends(TTSStoppedFrame))
    sound = [(t, f) for t, f in frames if isinstance(f, TTSAudioRawFrame)]
    if error or not sound:
        silent = not error or "no audio" in error  # what a refused key looks like on a socket
        return {"ok": False, "summary": NO_AUDIO if silent else error}
    rate = sound[0][1].sample_rate
    data = io.BytesIO()
    with wave.open(data, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"".join(f.audio for _, f in sound))
    first = sound[0][0]
    return {
        "ok": first <= SLOW_SECONDS,
        "summary": f"first sound in {first:.1f} s"
        + (" · too slow" if first > SLOW_SECONDS else ""),
        "audio": "data:audio/wav;base64," + base64.b64encode(data.getvalue()).decode(),
    }


class _Probe(FrameProcessor):
    """Keeps each frame with its time, and sets done on the frame that ends the test."""

    def __init__(self, ends: Callable[[Frame], bool]) -> None:
        super().__init__()
        self.frames: list[tuple[float, Frame]] = []
        self.done = asyncio.Event()
        self.start = time.monotonic()
        self._ends = ends

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        self.frames.append((time.monotonic() - self.start, frame))
        if self._ends(frame):
            self.done.set()
        await self.push_frame(frame, direction)


async def _run(
    service: FrameProcessor,
    feed: Sequence[Frame | float],
    ends: Callable[[Frame], bool],
    *,
    rate: int = 16_000,
    seconds: float = SECONDS,
) -> tuple[list[tuple[float, Frame]], str]:
    """Runs one service in a small pipeline: frames in, the probe after it. A number in the
    feed waits that many seconds. Gives the frames after the service and the first error."""
    errors = _Probe(lambda frame: isinstance(frame, ErrorFrame))
    probe = _Probe(ends)
    params = PipelineParams(audio_in_sample_rate=rate, audio_out_sample_rate=24_000)
    worker = PipelineWorker(
        Pipeline([errors, service, probe]), params=params, cancel_on_idle_timeout=False
    )
    started = asyncio.Event()

    @worker.event_handler("on_pipeline_started")
    async def _on_started(worker: PipelineWorker, frame: Frame) -> None:
        started.set()

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    running = asyncio.create_task(runner.run())
    try:
        await asyncio.wait_for(started.wait(), SECONDS)
        probe.start = time.monotonic()
        for item in feed:
            if isinstance(item, float):
                await asyncio.sleep(item)
            else:
                await worker.queue_frame(item)
        waits = [asyncio.ensure_future(event.wait()) for event in (probe.done, errors.done)]
        await asyncio.wait(waits, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
        for waiting in waits:
            waiting.cancel()
    finally:
        await runner.cancel()
        await running
    failure = next((str(f.error) for _, f in errors.frames if isinstance(f, ErrorFrame)), "")
    if not failure and not probe.done.is_set():
        failure = f"no answer in {seconds:.0f} s"
    return probe.frames, failure


def _ends(kind: type[Frame]) -> Callable[[Frame], bool]:
    return lambda frame: isinstance(frame, kind)


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z]+", text.lower())
