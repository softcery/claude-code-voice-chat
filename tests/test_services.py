from __future__ import annotations

from voice_relay.services import _marked


def test_cache_points_go_on_the_system_prompt_and_the_two_newest_texts() -> None:
    messages = [
        {"role": "system", "content": "prompt"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "reply"},
        {"role": "tool", "content": "result"},
        {"role": "user", "content": "new"},
    ]
    marked = [isinstance(message["content"], list) for message in _marked(messages)]
    assert marked == [True, False, True, False, True]
