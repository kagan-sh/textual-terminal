"""PTY terminal emulator for Textual 7.x.

Fork of mitosch/textual-terminal, refined for kagan-sh. Textual 7.x compatible:
uses app.theme instead of deprecated DEFAULT_COLORS and ColorSystem.
Original: https://github.com/mitosch/textual-terminal (LGPL-3.0-or-later).
Based on David Brochart's pyte example.
"""

from __future__ import annotations

from collections.abc import Callable

import asyncio
import fcntl
import os
import pty
import re
import shlex
import signal
import struct
import termios
from asyncio import Task
from pathlib import Path

import pyte
from pyte.screens import Char
from rich.color import ColorParseError
from rich.style import Style
from rich.text import Text

from textual import events, log
from textual.widget import Widget


class TerminalPyteScreen(pyte.Screen):
    """Overrides the pyte.Screen class to be used with TERM=linux."""

    def set_margins(self, *args, **kwargs):
        kwargs.pop("private", None)
        return super().set_margins(*args, **kwargs)


class TerminalDisplay:
    """Rich display for the terminal."""

    def __init__(self, lines):
        self.lines = lines

    def __rich_console__(self, _console, _options):
        line: Text
        for line in self.lines:
            yield line


_re_ansi_sequence = re.compile(r"(\x1b\[\??[\d;]*[a-zA-Z])")
DECSET_PREFIX = "\x1b[?"


class Terminal(Widget, can_focus=True):
    """Terminal textual widget. Textual 7.x compatible."""

    DEFAULT_CSS = """
    Terminal {
        background: $background;
    }
    """

    textual_colors: dict | None

    def __init__(
        self,
        command: str,
        default_colors: str | None = "system",
        on_escape: Callable[[], None] | None = None,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        self.command = command
        self.default_colors = default_colors
        self.on_escape = on_escape

        # Defer textual color detection until first use (app may not exist in __init__)
        self.textual_colors = None

        self.ncol = 80
        self.nrow = 24
        self.mouse_tracking = False

        self.emulator: TerminalEmulator | None = None
        self.send_queue: asyncio.Queue | None = None
        self.recv_queue: asyncio.Queue | None = None
        self.recv_task: Task | None = None

        self.ctrl_keys = {
            "up": "\x1bOA",
            "down": "\x1bOB",
            "right": "\x1bOC",
            "left": "\x1bOD",
            "home": "\x1bOH",
            "end": "\x1b[F",
            "delete": "\x1b[3~",
            "pageup": "\x1b[5~",
            "pagedown": "\x1b[6~",
            "shift+tab": "\x1b[Z",
            "f1": "\x1bOP",
            "f2": "\x1bOQ",
            "f3": "\x1bOR",
            "f4": "\x1bOS",
            "f5": "\x1b[15~",
            "f6": "\x1b[17~",
            "f7": "\x1b[18~",
            "f8": "\x1b[19~",
            "f9": "\x1b[20~",
            "f10": "\x1b[21~",
            "f11": "\x1b[23~",
            "f12": "\x1b[24~",
            "f13": "\x1b[25~",
            "f14": "\x1b[26~",
            "f15": "\x1b[28~",
            "f16": "\x1b[29~",
            "f17": "\x1b[31~",
            "f18": "\x1b[32~",
            "f19": "\x1b[33~",
            "f20": "\x1b[34~",
        }
        self._display = self.initial_display()
        self._screen = TerminalPyteScreen(self.ncol, self.nrow)
        self.stream = pyte.Stream(self._screen)

        super().__init__(name=name, id=id, classes=classes)

    def start(self) -> None:
        if self.emulator is not None:
            return

        self.emulator = TerminalEmulator(command=self.command)
        self.emulator.start()
        self.send_queue = self.emulator.recv_queue
        self.recv_queue = self.emulator.send_queue
        self.recv_task = asyncio.create_task(self.recv())

    def stop(self) -> None:
        if self.emulator is None:
            return

        self._display = self.initial_display()

        if self.recv_task is not None:
            self.recv_task.cancel()

        self.emulator.stop()
        self.emulator = None
        self.recv_task = None

    async def send_input(self, text: str) -> None:
        """Send text to the PTY (e.g. from external input widget)."""
        if self.send_queue is None:
            return
        await self.send_queue.put(["stdin", text])

    def render(self):
        return self._display

    async def on_key(self, event: events.Key) -> None:
        if self.emulator is None or self.send_queue is None:
            return

        if event.key == "ctrl+f1":
            self.app.set_focus(None)
            return

        if event.key == "escape" and self.on_escape is not None:
            event.stop()
            self.on_escape()
            return

        event.stop()
        char = self.ctrl_keys.get(event.key) or event.character
        if char:
            await self.send_queue.put(["stdin", char])

    async def on_resize(self, _event: events.Resize) -> None:
        if self.emulator is None or self.send_queue is None:
            return

        self.ncol = self.size.width
        self.nrow = self.size.height
        await self.send_queue.put(["set_size", self.nrow, self.ncol])
        self._screen.resize(self.nrow, self.ncol)

    async def on_click(self, event: events.MouseEvent):
        if self.emulator is None or self.send_queue is None:
            return

        if self.mouse_tracking is False:
            return

        await self.send_queue.put(["click", event.x, event.y, event.button])

    async def on_mouse_scroll_down(self, event: events.MouseScrollDown):
        if self.emulator is None or self.send_queue is None:
            return

        if self.mouse_tracking is False:
            return

        await self.send_queue.put(["scroll", "down", event.x, event.y])

    async def on_mouse_scroll_up(self, event: events.MouseScrollUp):
        if self.emulator is None or self.send_queue is None:
            return

        if self.mouse_tracking is False:
            return

        await self.send_queue.put(["scroll", "up", event.x, event.y])

    async def recv(self):
        if self.recv_queue is None or self.send_queue is None:
            return

        try:
            while True:
                message = await self.recv_queue.get()
                cmd = message[0]
                if cmd == "setup":
                    await self.send_queue.put(["set_size", self.nrow, self.ncol])
                elif cmd == "stdout":
                    chars = message[1]

                    for sep_match in re.finditer(_re_ansi_sequence, chars):
                        sequence = sep_match.group(0)
                        if sequence.startswith(DECSET_PREFIX):
                            parameters = sequence.removeprefix(DECSET_PREFIX).split(";")
                            if "1000h" in parameters:
                                self.mouse_tracking = True
                            if "1000l" in parameters:
                                self.mouse_tracking = False

                    try:
                        self.stream.feed(chars)
                    except TypeError as error:
                        log.warning("could not feed:", error)

                    lines = []
                    last_char: Char
                    last_style: Style
                    for y in range(self._screen.lines):
                        line_text = Text()
                        line = self._screen.buffer[y]
                        style_change_pos: int = 0
                        for x in range(self._screen.columns):
                            char: Char = line[x]

                            line_text.append(char.data)

                            if x > 0:
                                last_char = line[x - 1]
                                if (
                                    not self.char_style_cmp(char, last_char)
                                    or x == self._screen.columns - 1
                                ):
                                    last_style = self.char_rich_style(last_char)
                                    line_text.stylize(last_style, style_change_pos, x + 1)
                                    style_change_pos = x

                            if (
                                self._screen.cursor.x == x
                                and self._screen.cursor.y == y
                            ):
                                line_text.stylize("reverse", x, x + 1)

                        lines.append(line_text)

                    self._display = TerminalDisplay(lines)
                    self.refresh()

                elif cmd == "disconnect":
                    self.stop()
        except asyncio.CancelledError:
            pass

    def _get_textual_colors(self) -> dict:
        """Lazy-detect textual colors when app is available."""
        if self.textual_colors is None:
            self.textual_colors = self.detect_textual_colors()
        return self.textual_colors

    def char_rich_style(self, char: Char) -> Style:
        """Returns a rich.Style from the pyte.Char."""
        foreground = self.detect_color(char.fg)
        background = self.detect_color(char.bg)
        if self.default_colors == "textual":
            colors = self._get_textual_colors()
            if background == "default":
                background = colors["background"]
            if foreground == "default":
                foreground = colors["foreground"]

        style: Style
        try:
            style = Style(
                color=foreground,
                bgcolor=background,
                bold=char.bold,
            )
        except ColorParseError as error:
            log.warning("color parse error:", error)
            style = Style()

        return style

    def char_style_cmp(self, given: Char, other: Char) -> bool:
        """Compares two pyte.Chars and returns if these are the same."""
        return (
            given.fg == other.fg
            and given.bg == other.bg
            and given.bold == other.bold
            and given.italics == other.italics
            and given.underscore == other.underscore
            and given.strikethrough == other.strikethrough
            and given.reverse == other.reverse
            and given.blink == other.blink
        )

    def detect_color(self, color: str) -> str:
        """Tries to detect the correct Rich-Color based on a color name."""
        if color == "brown":
            return "yellow"

        if color == "brightblack":
            return "#808080"

        if re.match(r"[0-9a-f]{6}", color, re.IGNORECASE):
            return f"#{color}"

        return color

    def detect_textual_colors(self) -> dict:
        """Return colors from the active Textual theme."""
        theme = getattr(self.app, "current_theme", None)
        if theme is not None:
            return {
                "background": str(theme.background),
                "foreground": str(theme.foreground),
                "surface": str(theme.surface),
                "accent": str(theme.accent),
            }

        is_dark = "dark" in str(self.app.theme).lower()
        return {
            "background": "#000000" if is_dark else "#ffffff",
            "foreground": "#ffffff" if is_dark else "#000000",
            "surface": "#1e1e1e" if is_dark else "#f0f0f0",
            "accent": "#0178d4",
        }

    def initial_display(self) -> TerminalDisplay:
        """Returns the display when initially creating the terminal or clearing it."""
        return TerminalDisplay([Text()])


class TerminalEmulator:
    """PTY emulator for running a subprocess."""

    def __init__(self, command: str):
        self.ncol = 80
        self.nrow = 24
        self.data_or_disconnect = None
        self.run_task: asyncio.Task | None = None
        self.send_task: asyncio.Task | None = None

        self.fd = self.open_terminal(command=command)
        self.p_out = os.fdopen(self.fd, "w+b", 0)
        self.recv_queue: asyncio.Queue = asyncio.Queue()
        self.send_queue: asyncio.Queue = asyncio.Queue()
        self.event = asyncio.Event()

    def start(self):
        self.run_task = asyncio.create_task(self._run())
        self.send_task = asyncio.create_task(self._send_data())

    def stop(self):
        try:
            loop = asyncio.get_running_loop()
            loop.remove_reader(self.p_out)
        except (RuntimeError, OSError):
            pass
        if self.run_task is not None:
            self.run_task.cancel()
        if self.send_task is not None:
            self.send_task.cancel()
        try:
            os.kill(self.pid, signal.SIGTERM)
            os.waitpid(self.pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass

    def open_terminal(self, command: str):
        self.pid, fd = pty.fork()
        if self.pid == 0:
            argv = shlex.split(command)
            env = dict(
                TERM="xterm",
                LC_ALL="en_US.UTF-8",
                HOME=str(Path.home()),
                PYTHONUNBUFFERED="1",
            )
            os.execvpe(argv[0], argv, env)

        return fd

    async def _run(self):
        loop = asyncio.get_running_loop()

        def on_output():
            try:
                self.data_or_disconnect = self.p_out.read(4096).decode()
                self.event.set()
            except UnicodeDecodeError as error:
                log.warning("decode error:", error)
            except Exception:
                loop.remove_reader(self.p_out)
                self.data_or_disconnect = None
                self.event.set()

        loop.add_reader(self.p_out, on_output)
        await self.send_queue.put(["setup", {}])
        try:
            while True:
                msg = await self.recv_queue.get()
                if msg[0] == "stdin":
                    self.p_out.write(msg[1].encode())
                elif msg[0] == "set_size":
                    winsize = struct.pack("HH", msg[1], msg[2])
                    fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)
                elif msg[0] == "click":
                    x = msg[1] + 1
                    y = msg[2] + 1
                    button = msg[3]

                    if button == 1:
                        self.p_out.write(f"\x1b[<0;{x};{y}M".encode())
                        self.p_out.write(f"\x1b[<0;{x};{y}m".encode())
                elif msg[0] == "scroll":
                    x = msg[2] + 1
                    y = msg[3] + 1

                    if msg[1] == "up":
                        self.p_out.write(f"\x1b[<64;{x};{y}M".encode())
                    if msg[1] == "down":
                        self.p_out.write(f"\x1b[<65;{x};{y}M".encode())
        except asyncio.CancelledError:
            pass

    async def _send_data(self):
        try:
            while True:
                await self.event.wait()
                self.event.clear()
                if self.data_or_disconnect is not None:
                    await self.send_queue.put(["stdout", self.data_or_disconnect])
                else:
                    await self.send_queue.put(["disconnect", 1])
        except asyncio.CancelledError:
            pass
