"""The tray icon, a child process of the server. It exits when the server is gone."""

from __future__ import annotations

import sys
import threading
import time
from typing import Any

import httpx
import pystray
from PIL import Image, ImageDraw

from .settings import HOST, token

POLL_SECONDS = 2.0
MISSES = 3
SIZE = 64
COLORS = {
    "off": (142, 142, 147),
    "listening": (52, 199, 89),
    "working": (10, 132, 255),
    "waiting": (255, 159, 10),
    "problem": (255, 69, 58),
}
TITLES = {
    "off": "no voice window",
    "listening": "listening",
    "working": "Claude Code works",
    "waiting": "Claude Code waits for you",
    "problem": "set up on the settings page",
}


def main() -> None:
    port = int(sys.argv[1])
    Tray(f"http://{HOST}:{port}", token()).run()


class Tray:
    def __init__(self, base: str, secret: str) -> None:
        self._client = httpx.Client(
            base_url=base, headers={"authorization": f"Bearer {secret}"}, timeout=5
        )
        self._state: dict[str, Any] = {}
        self._icon = pystray.Icon(
            "voice-relay", _image(_mode(self._state), muted=False), "Voice relay",
            menu=pystray.Menu(self._items),
        )  # fmt: skip

    def run(self) -> None:
        if sys.platform == "darwin":
            _hide_dock_icon()
        self._icon.run(setup=self._setup)

    def _setup(self, icon: pystray.Icon) -> None:
        icon.visible = True
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self) -> None:
        misses = 0
        while misses < MISSES:
            try:
                state = self._client.get("/desktop/state").raise_for_status().json()
                misses = 0
            except httpx.HTTPError:
                misses += 1
                time.sleep(POLL_SECONDS)
                continue
            if state != self._state:
                self._show(state)
            time.sleep(POLL_SECONDS)
        self._icon.stop()

    def _show(self, state: dict[str, Any]) -> None:
        self._state = state
        mode = _mode(state)
        title = "muted" if state.get("muted") else TITLES[mode]
        self._icon.icon = _image(mode, muted=bool(state.get("muted")))
        self._icon.title = f"Voice relay: {title}"
        self._icon.update_menu()

    def _items(self) -> Any:
        state = self._state
        voice = bool(state.get("voice"))
        yield pystray.MenuItem(self._icon.title, None, enabled=False)
        sessions = state.get("sessions") or []
        for session in sessions:
            yield pystray.MenuItem(f"    {session['name']}: {_doing(session)}", None, enabled=False)
        if not sessions:
            yield pystray.MenuItem("    No Claude Code session linked", None, enabled=False)
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem(
            "Voice window is open" if voice else "Open voice window",
            lambda: self._post("/desktop/open", {"page": "client"}),
            enabled=not voice, default=True,
        )  # fmt: skip
        yield pystray.MenuItem(
            "Mute microphone",
            lambda: self._post("/desktop/mute", {"muted": not self._state.get("muted")}),
            checked=lambda item: bool(self._state.get("muted")),
        )
        yield pystray.MenuItem(
            "Settings", lambda: self._post("/desktop/open", {"page": "settings"})
        )
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem("Quit voice relay", self._quit)

    def _post(self, path: str, body: dict[str, Any]) -> None:
        try:
            self._client.post(path, json=body).raise_for_status()
        except httpx.HTTPError as error:
            print(f"voice-relay tray: {path} failed: {error}", file=sys.stderr, flush=True)

    def _quit(self) -> None:
        self._post("/desktop/quit", {})
        self._icon.stop()


def _mode(state: dict[str, Any]) -> str:
    sessions = state.get("sessions") or []
    if state.get("problems"):
        return "problem"
    if any(session["waiting"] for session in sessions):
        return "waiting"
    if any(session["running"] for session in sessions):
        return "working"
    return "listening" if state.get("voice") else "off"


def _doing(session: dict[str, Any]) -> str:
    if session["waiting"]:
        return "waits for approval"
    return "works" if session["running"] else "idle"


def _image(mode: str, *, muted: bool) -> Image.Image:
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 8, SIZE - 8, SIZE - 8), fill=COLORS["off"] if muted else COLORS[mode])
    if muted:
        draw.line((12, SIZE - 12, SIZE - 12, 12), fill=(255, 255, 255), width=7)
    return image


def _hide_dock_icon() -> None:
    """Keeps the tray process out of the macOS Dock."""
    import AppKit  # type: ignore[import-not-found]

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)


if __name__ == "__main__":
    main()
