"""A new terminal running Claude Code in a project folder found by its spoken name.
VOICE_RELAY_LINK tells the mod in it to link."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import uuid
from collections.abc import Iterable
from pathlib import Path

from .sessions import key
from .settings import CONFIG_DIR

LINK = "VOICE_RELAY_LINK"
LISTED = 20
LINUX_TERMINALS = (
    ("x-terminal-emulator", "-e"), ("gnome-terminal", "--"), ("konsole", "-e"),
    ("xfce4-terminal", "-x"), ("kitty", ""), ("alacritty", "-e"), ("xterm", "-e"),
)  # fmt: skip


def choose(name: str, roots: Iterable[Path]) -> Path | str:
    """Gives the one folder in the roots named so, or that contains the name; else the reason."""
    candidates = _folders(roots)
    if not candidates:
        return (
            "no projects folder is set and no linked session shows where the projects are. "
            "Tell the operator to set the projects folder on the settings page."
        )
    wanted = key(name)
    found = [folder for folder in candidates if key(folder.name) == wanted]
    if not found and wanted:
        found = [folder for folder in candidates if wanted in key(folder.name)]
    if len(found) == 1:
        return found[0]
    names = ", ".join(folder.name for folder in (found or candidates)[:LISTED])
    if found:
        return f"{len(found)} folders match {name}: {names}. Ask the operator which one."
    return f"no project folder is named {name}. The folders: {names}."


def open_claude(folder: Path, *, resume: bool = False, terminal: str = "") -> None:
    """Starts Claude Code, with its last conversation in the folder when resume is set. The
    terminal is a command that runs its last argument with sh -c, for example `kitty`; empty
    means the system terminal. Raises OSError when no terminal starts."""
    claude = "claude --continue" if resume else "claude"
    command = f"cd {shlex.quote(str(folder))} && {LINK}=1 exec {claude}"
    if terminal and sys.platform != "win32":
        try:
            subprocess.Popen([*shlex.split(terminal), "sh", "-c", command])
        except (OSError, ValueError) as error:
            kind = type(error).__name__
            raise OSError(f"VOICE_RELAY_TERMINAL: expected a command, got {kind}") from None
        return
    if sys.platform == "darwin":
        script = CONFIG_DIR / f"open-{uuid.uuid4().hex[:8]}.command"
        script.write_text(f'#!/bin/sh\nrm -f -- "$0"\n{command}\n', encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        try:
            subprocess.run(["open", str(script)], check=True, capture_output=True, timeout=15)
        except subprocess.SubprocessError as error:
            raise OSError(f"open: expected exit code 0, got {type(error).__name__}") from error
        return
    if sys.platform == "win32":
        subprocess.Popen(
            ["cmd", "/c", "start", "Claude Code", "/D", str(folder), "cmd", "/k", claude],
            env={**os.environ, LINK: "1"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return
    for name, flag in LINUX_TERMINALS:
        found = shutil.which(name)
        if found:
            subprocess.Popen([found, *([flag] if flag else []), "sh", "-c", command])
            return
    raise OSError("no terminal found: expected x-terminal-emulator, gnome-terminal or xterm")


def trusted(folder: Path) -> bool:
    """True when Claude Code trusts the folder or a folder above it, so it opens with no
    question. The trust lives in .claude.json of the Claude Code config."""
    home = os.environ.get("CLAUDE_CONFIG_DIR")
    config = Path(home) / ".claude.json" if home else Path.home() / ".claude.json"
    try:
        projects = json.loads(config.read_text(encoding="utf-8")).get("projects", {})
    except (OSError, ValueError, AttributeError):
        return False
    real = folder.resolve()
    return any(
        projects.get(str(path), {}).get("hasTrustDialogAccepted") for path in (real, *real.parents)
    )


def _folders(roots: Iterable[Path]) -> list[Path]:
    found: dict[Path, None] = {}
    for root in roots:
        if root.is_dir():
            for child in root.iterdir():
                if child.is_dir() and not child.name.startswith("."):
                    found[child.resolve()] = None
    return sorted(found, key=lambda folder: folder.name.lower())
