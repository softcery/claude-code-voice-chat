"""The voice side of the link: the tools the voice model calls, and the Claude Code news it
hears. Progress waits for a quiet conversation and the narration interval."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.frames.frames import LLMMessagesAppendFrame
from pipecat.pipeline.worker import PipelineWorker
from pipecat.services.llm_service import FunctionCallParams, LLMService

from . import settings, terminal
from .sessions import ABORT, TASK, Session, Sessions

DEVELOPER = "developer"
ANSWER = "Claude Code in {name} finished its turn. Its final text:\n\n{answer}"
STOPPED = "(The operator stopped this turn before it finished.)"  # the mod's ABORTED
PROGRESS = "Claude Code progress since the last update:\n\n{progress}"
HISTORY = "Claude Code conversation so far in {name}:\n\n<conversation>\n{history}\n</conversation>"
WAITING = "Claude Code in {name} waits at the screen for the operator to approve: {action}"
TYPED = "The operator typed this to Claude Code in {name} directly:\n\n{text}"
LINKED = "Claude Code sessions linked now: {names}."
QUOTE = 'The operator says, through the voice link: "{text}"'
QUIET_SECONDS = 1.5
REPLY_SECONDS = 4.0  # a message that runs the LLM: its spoken reply starts within this time
RESUMED = "Claude Code and its last conversation"
OPENED = "opened a new terminal with {what} in {folder}. It links in a few seconds as {folder}."
UNTRUSTED = (
    " Claude Code never ran in this folder: it asks at the screen to trust the folder first."
)

SESSION = {
    "type": "string",
    "description": "The name of the Claude Code session. Leave empty when one session is linked.",
}
SEND_TASK = FunctionSchema(
    name="send_task",
    description=(
        "Sends what the operator said to one Claude Code session, as a quote. "
        "Pass their words as said. Fix only speech recognition errors and drop filler "
        "sounds such as uh and um. The answer arrives later as a developer message."
    ),
    properties={
        "text": {"type": "string", "description": "The operator's words."},
        "session": SESSION,
    },
    required=["text"],
)
ABORT_TURN = FunctionSchema(
    name="abort",
    description="Stops the Claude Code turn that runs now in one session.",
    properties={
        "session": SESSION,
        "then": {
            "type": "string",
            "description": "What the operator wants instead, in their words, when they gave it. "
            "It goes to the session right after the stop.",
        },
    },
    required=[],
)
OPEN_SESSION = FunctionSchema(
    name="open_session",
    description=(
        "Opens a new terminal window with Claude Code in a project folder, and links it to the "
        "voice link. A task given here goes to it once it links."
    ),
    properties={
        "folder": {"type": "string", "description": "The project folder name the operator said."},
        "task": {
            "type": "string",
            "description": "Work the operator asked the new session to do, in their words. Not "
            "the description of which session to open.",
        },
        "resume": {
            "type": "boolean",
            "description": "True to continue the last conversation in the folder, when the "
            "operator asks for an earlier session; false for a new conversation.",
        },
    },
    required=["folder"],
)
TOOLS = (SEND_TASK, ABORT_TURN, OPEN_SESSION)


class Bridge:
    """The linked sessions, and the one live voice worker that hears about them."""

    def __init__(self, *, interval: float) -> None:
        self.sessions = Sessions()
        self.interval = interval
        self._worker: PipelineWorker | None = None
        self._progress: list[tuple[Session, str]] = []
        self._narrated = 0.0
        self._asked = 0.0
        self._narration: asyncio.Task[None] | None = None
        self._quiet = asyncio.Event()
        self._quiet.set()
        self._notices: set[asyncio.Task[bool]] = set()

    def register(self, llm: LLMService[Any]) -> None:
        llm.register_function(SEND_TASK.name, self._send_task)
        llm.register_function(ABORT_TURN.name, self._abort)
        llm.register_function(OPEN_SESSION.name, self._open_session)

    def attach(self, worker: PipelineWorker) -> None:
        self._worker = worker
        for session in self.sessions.all():
            session.want_history()
        tracker = worker.turn_tracking_observer
        if tracker is None:
            return

        @tracker.event_handler("on_turn_started")
        async def _on_turn_started(observer: Any, turn: int) -> None:
            if self._worker is worker:
                self._quiet.clear()

        @tracker.event_handler("on_turn_ended")
        async def _on_turn_ended(observer: Any, turn: int, seconds: float, cut: bool) -> None:
            if self._worker is worker:
                self._quiet.set()

    def detach(self, worker: PipelineWorker) -> None:
        if self._worker is worker:
            self._worker = None
            self._quiet.set()
            self._drop()

    def voice(self) -> bool:
        return self._worker is not None

    def active(self) -> bool:
        """True while a voice client is connected or a session polls. Drops gone sessions."""
        if self.sessions.prune():
            self._notify(self.linked_line())
        return self._worker is not None or bool(self.sessions.live())

    def linked_line(self) -> str:
        names = ", ".join(session.name for session in self.sessions.all())
        return LINKED.format(names=names or "none")

    def state(self) -> list[dict[str, Any]]:
        return [
            {"name": session.name, "running": session.running, "waiting": session.waiting}
            for session in self.sessions.all()
        ]

    async def link(self, session_id: str, name: str, folder: str) -> str:
        session, new = self.sessions.link(session_id, name, folder)
        if new and self._worker is not None:
            session.want_history()
            await self._append(self.linked_line(), run=False)
        if new:
            self.sessions.release(session)
        return session.name

    async def unlink(self, session_id: str) -> None:
        if self.sessions.unlink(session_id):
            await self._append(self.linked_line(), run=False)

    def progress(self, session: Session, line: str) -> None:
        session.running, session.waiting = True, False
        self._progress.append((session, line))
        if self._narration is None or self._narration.done():
            self._narration = asyncio.create_task(self._narrate())

    async def history(self, session: Session, text: str) -> bool:
        return await self._append(HISTORY.format(name=session.name, history=text), run=False)

    async def typed(self, session: Session, text: str) -> bool:
        session.running = True
        return await self._append(TYPED.format(name=session.name, text=text), run=False)

    async def answer(self, session: Session, text: str) -> bool:
        stopped, restarting = session.stopping and text == STOPPED, session.restarting
        session.stopping = session.restarting = False
        session.running, session.waiting = stopped and restarting, False  # the next task waits
        self._drop(session)
        if stopped:  # the operator asked for it and heard it confirmed
            return True
        return await self._append(ANSWER.format(name=session.name, answer=text))

    async def waiting(self, session: Session, action: str) -> bool:
        session.waiting = True
        self._drop(session)
        return await self._append(WAITING.format(name=session.name, action=action))

    async def _send_task(self, params: FunctionCallParams) -> None:
        found = self.sessions.find(str(params.arguments.get("session") or ""))
        if isinstance(found, str):
            await params.result_callback(f"not sent: {found}")
            return
        busy, found.running = found.running, True
        found.send(TASK, QUOTE.format(text=str(params.arguments.get("text", ""))))
        after = ", Claude Code starts it after the current turn" if busy else ""
        await params.result_callback(f"sent to {found.name}{after}")

    async def _abort(self, params: FunctionCallParams) -> None:
        found = self.sessions.find(str(params.arguments.get("session") or ""))
        if isinstance(found, str):
            await params.result_callback(f"not sent: {found}")
            return
        found.stopping = True
        found.send(ABORT)
        then = str(params.arguments.get("then") or "").strip()
        if then:
            found.running = found.restarting = True
            found.send(TASK, QUOTE.format(text=then))
        sent = ", and the new instructions after it" if then else ""
        await params.result_callback(f"abort sent to {found.name}{sent}")

    async def _open_session(self, params: FunctionCallParams) -> None:
        name = str(params.arguments.get("folder") or "")
        task = str(params.arguments.get("task") or "").strip()
        resume = params.arguments.get("resume") is True
        folder = terminal.choose(name, self._roots())
        if isinstance(folder, str):
            await params.result_callback(f"not opened: {folder}")
            return
        try:
            command = settings.environment().get("VOICE_RELAY_TERMINAL", "").strip()
            await asyncio.to_thread(terminal.open_claude, folder, resume=resume, terminal=command)
        except OSError as error:
            await params.result_callback(f"not opened: {error}")
            return
        if task:
            self.sessions.hold(folder, QUOTE.format(text=task))
        held = " The task goes to it once it links." if task else ""
        opened = OPENED.format(folder=folder.name, what=RESUMED if resume else "Claude Code")
        trust = "" if terminal.trusted(folder) else UNTRUSTED
        await params.result_callback(opened + trust + held)

    def _roots(self) -> list[Path]:
        projects = settings.environment().get("VOICE_RELAY_PROJECTS", "").strip()
        roots = [Path(projects).expanduser()] if projects else []
        return roots + [folder.parent for folder in self.sessions.folders()]

    async def _narrate(self) -> None:
        await asyncio.sleep(max(0.0, self._narrated + self.interval - time.monotonic()))
        # wait for a lasting pause, after the spoken reply to the last message began:
        # the operator can be about to speak, and a reply still in the LLM misses this progress
        while True:
            await self._quiet.wait()
            await asyncio.sleep(max(QUIET_SECONDS, self._asked + REPLY_SECONDS - time.monotonic()))
            if self._quiet.is_set() and time.monotonic() - self._asked >= REPLY_SECONDS:
                break
        lines, self._progress = self._progress, []
        text = "\n".join(f"{session.name}: {line}" for session, line in lines)
        if lines and await self._append(PROGRESS.format(progress=text)):
            self._narrated = time.monotonic()

    def _drop(self, session: Session | None = None) -> None:
        self._progress = [held for held in self._progress if session not in (None, held[0])]
        if not self._progress and self._narration is not None:
            self._narration.cancel()
            self._narration = None

    def _notify(self, content: str) -> None:
        """Appends from sync code; the set holds each task until it ends."""
        if self._worker is None:
            return
        task = asyncio.create_task(self._append(content, run=False))
        self._notices.add(task)
        task.add_done_callback(self._notices.discard)

    async def _append(self, content: str, *, run: bool = True) -> bool:
        worker = self._worker
        if worker is None:
            return False
        message: Any = {"role": DEVELOPER, "content": content}
        if run:
            self._asked = time.monotonic()
        await worker.queue_frame(LLMMessagesAppendFrame(messages=[message], run_llm=run))
        return True
