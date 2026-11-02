# Security

## Report a vulnerability

Do not open a public issue for a security problem. Report it privately: on the [Security tab](https://github.com/softcery/claude-code-voice-chat/security), press **Report a vulnerability**. Add the steps to reproduce it and the effect.

We answer within 5 working days.

## Scope

In scope:
- The voice server: its routes, the `Origin` and `Host` guard, the token checks.
- The mod in Claude Code and what it submits as a prompt.
- How keys and settings are stored in `~/.config/voice-relay/`.

Out of scope:
- Problems in Claude Code, pipecat or a provider's service. Report those to their owners.
- An attack that needs local access as the same user, which already reads the keys.

## Supported versions

The latest release gets fixes.
