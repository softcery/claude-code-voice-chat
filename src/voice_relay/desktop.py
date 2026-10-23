"""The server as a desktop process: detached start, idle stop, log, tray and voice window.
No pipecat import, so `voice-relay start` stays fast."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import webbrowser
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from loguru import logger

from . import settings
from .settings import CONFIG_DIR, HOST

LOG_FILE = CONFIG_DIR / "server.log"
START_LOG = CONFIG_DIR / "start.log"
TRAY_LOG = CONFIG_DIR / "tray.log"
START_SECONDS = 180.0
IDLE_SECONDS = 600.0
CHECK_SECONDS = 15.0
WINDOW_SIZE = "--window-size=460,760"
MAC_BROWSERS = ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium")
WINDOWS_BROWSERS = (
    ("PROGRAMFILES(X86)", "Microsoft/Edge/Application/msedge.exe"),
    ("PROGRAMFILES", "Microsoft/Edge/Application/msedge.exe"),
    ("PROGRAMFILES", "Google/Chrome/Application/chrome.exe"),
    ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe"),
    ("PROGRAMFILES", "BraveSoftware/Brave-Browser/Application/brave.exe"),
)
LINUX_BROWSERS = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "microsoft-edge", "brave-browser",
)  # fmt: skip


def launch() -> int:
    """Starts the server detached and waits until it answers. Gives the exit code."""
    base = f"http://{HOST}:{settings.port()}"
    if _healthy(base):
        print(f"voice-relay: running at {base}")
        return 0
    START_LOG.parent.mkdir(parents=True, exist_ok=True)
    with START_LOG.open("w", encoding="utf-8") as log:
        child = _detached([sys.executable, "-m", "voice_relay.run"], log)
    deadline = time.monotonic() + START_SECONDS
    while time.monotonic() < deadline:
        if child.poll() is not None:
            tail = START_LOG.read_text(encoding="utf-8").strip().splitlines()[-5:]
            print("voice-relay: the server stopped at start:", *tail, sep="\n", file=sys.stderr)
            return 1
        if _healthy(base):
            print(f"voice-relay: started at {base}")
            return 0
        time.sleep(0.3)
    print(f"voice-relay: no answer at {base} in {START_SECONDS:.0f} s", file=sys.stderr)
    return 1


def lifespan(app: FastAPI, *, port: int, active: Callable[[], bool]) -> None:
    """Adds the log file, the tray process and the idle stop for the app's life."""
    outer = app.router.lifespan_context

    @asynccontextmanager
    async def desktop(app: FastAPI) -> AsyncIterator[None]:
        logger.add(LOG_FILE, level="DEBUG", rotation="20 MB", retention=2, enqueue=True)
        logging.getLogger("uvicorn.access").disabled = True  # the tray polls every 2 s
        tray = _tray(port)
        idle = asyncio.create_task(_idle_stop(active))
        try:
            yield
        finally:
            idle.cancel()
            if tray is not None and tray.poll() is None:
                tray.terminate()

    @asynccontextmanager
    async def both(app: FastAPI) -> AsyncIterator[None]:
        async with outer(app), desktop(app):
            yield

    app.router.lifespan_context = both


def show(url: str, *, window: bool) -> None:
    """Opens a Chrome or Edge app window when asked and found, else the default browser."""
    command = _app_window(url) if window else None
    if command is not None:
        try:
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except OSError as error:
            logger.warning("voice-relay: the app window did not open: {}", error)
    webbrowser.open(url)


def stop() -> None:
    signal.raise_signal(signal.SIGINT)


async def _idle_stop(active: Callable[[], bool]) -> None:
    since = time.monotonic()
    while True:
        await asyncio.sleep(CHECK_SECONDS)
        if active():
            since = time.monotonic()
        elif time.monotonic() - since > IDLE_SECONDS:
            logger.info("voice-relay: no session and no voice client, stopping")
            stop()
            return


def _tray(port: int) -> subprocess.Popen[bytes] | None:
    if settings.environment().get("VOICE_RELAY_TRAY", "").lower() == "off":
        return None
    try:
        with TRAY_LOG.open("a", encoding="utf-8") as log:
            return subprocess.Popen(
                [sys.executable, "-m", "voice_relay.tray", str(port)],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
    except OSError as error:
        logger.warning("voice-relay: the tray did not start: {}", error)
        return None


def _app_window(url: str) -> list[str] | None:
    flags = [
        f"--app={url}?window=1",
        WINDOW_SIZE,
    ]  # the page sizes itself: Chrome may ignore the flag
    if sys.platform == "darwin":
        for name in MAC_BROWSERS:
            for folder in (Path("/Applications"), Path.home() / "Applications"):
                if (folder / f"{name}.app").exists():
                    return ["open", "-na", str(folder / f"{name}.app"), "--args", *flags]
        return None
    if sys.platform == "win32":
        for variable, path in WINDOWS_BROWSERS:
            root = os.environ.get(variable)
            if root and (Path(root) / path).exists():
                return [str(Path(root) / path), *flags]
        return None
    found = next(filter(None, map(shutil.which, LINUX_BROWSERS)), None)
    return [found, *flags] if found else None


def _detached(command: list[str], log: Any) -> subprocess.Popen[bytes]:
    """Starts a process that outlives this one and the Claude Code session above it."""
    streams: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": log}
    # the log is a file, so Windows Python would encode it as cp1252 and crash on banners
    streams["env"] = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    if sys.platform != "win32":
        return subprocess.Popen(command, start_new_session=True, **streams)
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        return subprocess.Popen(
            command, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **streams
        )
    except OSError:
        return subprocess.Popen(command, creationflags=flags, **streams)


def _healthy(base: str) -> bool:
    try:
        reply = httpx.get(f"{base}/claude/health", timeout=2)
    except httpx.HTTPError:
        return False
    return reply.status_code == 200 and "voice-relay" in reply.text
