# Contributing

Bug reports, ideas and pull requests are welcome.

## Report a bug or ask for a feature

Open an [issue](https://github.com/softcery/claude-code-voice-relay/issues/new/choose) and pick the form. For a bug, add your system, the Claude Code version and the last lines of `~/.config/voice-relay/server.log`. Remove keys and private paths from the log first.

## Set up

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Claude Code 2.1.303 or later.
2. Clone the repository and run `uv sync`.
3. Run `claude --plugin-dir .` to load the mod from your checkout, then `/voice-relay`.

## Before a pull request

Run these checks. Each must pass:

```
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
claude plugin test .
claude plugin validate .
```

- Keep one change per pull request.
- Write the commit title as a [conventional commit](https://www.conventionalcommits.org/): `fix: …`, `feat: …`, `docs: …`.
- Add a test for a change in behaviour. A change of text or configuration needs none.
- Say in the pull request what you changed, why, and how you checked it.

## Layout

| Path | What it holds |
| --- | --- |
| `hooks/register.ts` | The mod in each Claude Code session: links it, takes tasks, reports progress and answers |
| `src/voice_relay/` | The pipecat voice server, its routes, the bridge to the sessions and the settings |
| `src/voice_relay/prompts/` | The voice agent's prompt |
| `src/voice_relay/web/` | The voice window and the settings page |
| `tests/` | pytest for the server, `claude plugin test` for the mod |

## Code of conduct

Be respectful. This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
