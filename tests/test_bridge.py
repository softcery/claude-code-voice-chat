from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from voice_relay import bridge as bridge_module
from voice_relay import routes, sessions, terminal
from voice_relay.bridge import QUOTE, STOPPED, Bridge
from voice_relay.processors import Microphone
from voice_relay.sessions import ABORT, CONTEXT, TASK

TOKEN = "secret"
BASE = "http://127.0.0.1:7860"
HEADERS = {"authorization": f"Bearer {TOKEN}", "x-claude-session": "session-1"}


class Worker:
    turn_tracking_observer = None

    def __init__(self) -> None:
        self.frames: list[Any] = []

    async def queue_frame(self, frame: Any) -> None:
        self.frames.append(frame)


class Call:
    def __init__(self, **arguments: Any) -> None:
        self.arguments = arguments
        self.result: Any = None

    async def result_callback(self, result: Any) -> None:
        self.result = result


async def voiced(*names: str) -> tuple[Bridge, Worker]:
    """A bridge with a voice worker and one linked session per name, history requests taken."""
    bridge, worker = Bridge(interval=0), Worker()
    bridge.attach(worker)  # type: ignore[arg-type]
    for name in names:
        await bridge.link(name, name, "")
        assert (await bridge.sessions[name].next(1)) == {"kind": CONTEXT, "text": ""}
    worker.frames.clear()
    return bridge, worker


async def test_a_task_goes_to_the_only_session_without_a_name() -> None:
    bridge = Bridge(interval=0)
    call = Call(text="run the lint")
    await bridge._send_task(call)  # type: ignore[arg-type]
    assert call.result.startswith("not sent: no Claude Code session")

    bridge, _ = await voiced("shop")
    await bridge._send_task(call)  # type: ignore[arg-type]
    assert (await bridge.sessions["shop"].next(1)) == {
        "kind": TASK,
        "text": QUOTE.format(text="run the lint"),
    }
    await bridge._abort(Call(then="fix only the lint"))  # type: ignore[arg-type]
    assert (await bridge.sessions["shop"].next(1)) == {"kind": ABORT, "text": ""}
    assert (await bridge.sessions["shop"].next(1)) == {
        "kind": TASK,
        "text": QUOTE.format(text="fix only the lint"),
    }
    assert await bridge.answer(bridge.sessions["shop"], STOPPED)
    assert bridge.sessions["shop"].running  # the new instructions wait to run


async def test_with_two_sessions_a_task_needs_a_name_and_reaches_that_session_only() -> None:
    bridge, _ = await voiced("shop", "voice-relay")
    unnamed, unknown = Call(text="run the tests"), Call(text="run the tests", session="billing")
    await bridge._send_task(unnamed)  # type: ignore[arg-type]
    await bridge._send_task(unknown)  # type: ignore[arg-type]
    assert "shop, voice-relay" in unnamed.result and "billing" in unknown.result

    await bridge._send_task(Call(text="run the tests", session="Voice Relay"))  # type: ignore[arg-type]
    assert (await bridge.sessions["voice-relay"].next(1))["kind"] == TASK  # type: ignore[index]
    assert (await bridge.sessions["shop"].next(0.01)) is None


async def test_an_answer_drops_only_its_session_progress() -> None:
    bridge, worker = await voiced("shop", "api")
    bridge.interval = 60
    bridge.progress(bridge.sessions["shop"], "runs make lint")
    bridge.progress(bridge.sessions["api"], "reads the log")
    assert await bridge.answer(bridge.sessions["shop"], "done")
    bridge.interval, bridge._narrated = 0, 0.0
    await asyncio.sleep(0)
    (answer,) = worker.frames
    assert answer.run_llm and answer.messages[0]["content"].startswith(
        "Claude Code in shop finished"
    )
    assert bridge.state() == [
        {"name": "shop", "running": False, "waiting": False},
        {"name": "api", "running": True, "waiting": False},
    ]
    assert [line for _, line in bridge._progress] == ["reads the log"]


async def test_progress_from_two_sessions_speaks_in_one_narration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_module, "QUIET_SECONDS", 0)
    bridge, worker = await voiced("shop", "api")
    bridge.progress(bridge.sessions["shop"], "runs make lint")
    bridge.progress(bridge.sessions["api"], "reads the log")
    await asyncio.sleep(0.01)
    (frame,) = worker.frames
    assert "shop: runs make lint\napi: reads the log" in frame.messages[0]["content"]


async def test_progress_waits_for_the_reply_to_an_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge_module, "QUIET_SECONDS", 0)
    monkeypatch.setattr(bridge_module, "REPLY_SECONDS", 0.05)
    bridge, worker = await voiced("shop", "api")
    assert await bridge.answer(bridge.sessions["shop"], "done")
    bridge.progress(bridge.sessions["api"], "reads the log")
    await asyncio.sleep(0.01)
    assert len(worker.frames) == 1
    await asyncio.sleep(0.1)
    assert "api: reads the log" in worker.frames[1].messages[0]["content"]


async def test_open_session_opens_the_spoken_folder_and_its_link_gets_the_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("shop", "voice-relay", "voice-notes"):
        (tmp_path / name).mkdir()
    opened: list[tuple[Path, bool]] = []
    monkeypatch.setattr(
        terminal, "open_claude", lambda folder, **how: opened.append((folder, how["resume"]))
    )
    bridge, _ = await voiced()
    await bridge.link("shop", "shop", str(tmp_path / "shop"))

    several = Call(folder="voice")
    await bridge._open_session(several)  # type: ignore[arg-type]
    assert "voice-notes, voice-relay" in several.result
    await bridge._open_session(Call(folder="Voice Relay", task="run the tests", resume=True))  # type: ignore[arg-type]
    assert opened == [((tmp_path / "voice-relay").resolve(), True)]

    await bridge.link("new", "voice-relay", str(tmp_path / "voice-relay"))
    assert (await bridge.sessions["new"].next(1)) == {"kind": CONTEXT, "text": ""}
    assert (await bridge.sessions["new"].next(1)) == {
        "kind": TASK,
        "text": QUOTE.format(text="run the tests"),
    }


async def test_routes_refuse_other_websites_a_wrong_token_and_an_unknown_session() -> None:
    app = FastAPI()
    bridge = Bridge(interval=0)
    routes.mount(app, bridge=bridge, microphone=Microphone(), token=TOKEN, port=7860, poll=0.05)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        body = {"name": "shop", "cwd": ""}
        evil = {**HEADERS, "origin": "https://evil.example"}
        assert (await client.post("/claude/link", headers=evil, json=body)).status_code == 403
        rebound = {**HEADERS, "host": "evil.example:7860"}
        assert (await client.get("/claude/health", headers=rebound)).status_code == 403
        wrong = {**HEADERS, "authorization": "Bearer other"}
        assert (await client.post("/claude/link", headers=wrong, json=body)).status_code == 401
        assert (await client.get("/claude/next", headers=HEADERS)).status_code == 409

        assert (await client.post("/claude/link", headers=HEADERS, json=body)).json() == {
            "name": "shop"
        }
        other = {**HEADERS, "x-claude-session": "session-2"}
        assert (await client.post("/claude/link", headers=other, json=body)).json() == {
            "name": "shop 2"
        }
        assert (await client.get("/claude/next", headers=HEADERS)).status_code == 204
        answer = await client.post("/claude/answer", headers=HEADERS, json={"answer": "ok"})
        assert answer.status_code == 202  # no voice client: kept out of the 409 that relinks
        broken = await client.post("/claude/answer", headers=HEADERS, content=b"{not json")
        assert broken.status_code == 400
        assert (await client.post("/claude/unlink", headers=HEADERS)).status_code == 204
        assert (await client.get("/claude/next", headers=HEADERS)).status_code == 409


async def test_a_held_task_expires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessions, "HOLD_SECONDS", 0.0)
    bridge, _ = await voiced()
    bridge.sessions.hold(tmp_path, "run the tests")
    await bridge.link("late", "late", str(tmp_path))
    assert (await bridge.sessions["late"].next(1))["kind"] == CONTEXT  # type: ignore[index]
    assert (await bridge.sessions["late"].next(0.01)) is None
