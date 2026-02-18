# textual-terminal (kagan-sh fork)

PTY terminal widget for [Textual](https://github.com/Textualize/textual) 7.x.

Fork of [mitosch/textual-terminal](https://github.com/mitosch/textual-terminal), refined for:
- **Textual 7.x compatibility** – removed deprecated `DEFAULT_COLORS` and `ColorSystem`; uses `app.theme`
- **Python 3.12+** – aligned with kagan-sh requirements
- **kagan TUI** – chat overlay PTY embedding

**This repo is separate from kagan.** All changes to textual-terminal happen here; kagan only consumes via git dependency.

## Installation

```bash
uv add "textual-terminal @ git+https://github.com/kagan-sh/textual-terminal.git@main"
```

Or in `pyproject.toml`:

```toml
[tool.uv.sources]
textual-terminal = { git = "https://github.com/kagan-sh/textual-terminal.git", branch = "main" }

[project]
dependencies = ["textual-terminal"]
```

## Usage

```python
from textual_terminal import Terminal

class TerminalApp(App):
    def compose(self) -> ComposeResult:
        yield Terminal(command="htop", id="terminal_htop")

    def on_ready(self) -> None:
        terminal: Terminal = self.query_one("#terminal_htop")
        terminal.start()
```

### `default_colors`

Use `default_colors="textual"` to match Textual theme:

```python
Terminal(command="htop", default_colors="textual")
```

## License

LGPL-3.0-or-later. Original by [Mischa Schindowski](https://github.com/mitosch). Based on [David Brochart](https://github.com/davidbrochart)'s pyte example.
