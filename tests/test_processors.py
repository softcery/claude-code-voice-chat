from __future__ import annotations

from pipecat.frames.frames import (
    FunctionCallFromLLM,
    FunctionCallsStartedFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.tests.utils import run_test

from voice_relay.processors import Preamble

CALL = FunctionCallFromLLM(function_name="send_task", tool_call_id="1", arguments={}, context=None)


async def spoken(*frames) -> str:
    down, _ = await run_test(Preamble(), frames_to_send=list(frames))
    return "".join(frame.text for frame in down if isinstance(frame, LLMTextFrame))


async def test_text_before_a_tool_call_is_dropped() -> None:
    text = await spoken(
        LLMFullResponseStartFrame(),
        LLMTextFrame("I'll send both "),
        LLMTextFrame("parts as one task."),
        FunctionCallsStartedFrame(function_calls=[CALL]),
        LLMFullResponseEndFrame(),
    )
    assert text == ""


async def test_a_reply_without_a_tool_call_is_spoken_whole() -> None:
    text = await spoken(
        LLMFullResponseStartFrame(),
        LLMTextFrame("Yes. "),
        LLMTextFrame("It runs the tests."),
        LLMFullResponseEndFrame(),
        LLMFullResponseStartFrame(),
        LLMTextFrame("Sent."),
        LLMFullResponseEndFrame(),
    )
    assert text == "Yes. It runs the tests.Sent."


async def test_a_long_answer_streams_before_its_end() -> None:
    long = "Here is the full text. " * 20
    down, _ = await run_test(
        Preamble(),
        frames_to_send=[LLMFullResponseStartFrame(), LLMTextFrame(long), LLMTextFrame("End.")],
    )
    assert [frame.text for frame in down if isinstance(frame, LLMTextFrame)] == [long, "End."]
