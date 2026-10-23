from __future__ import annotations

from voice_relay import settings


def test_your_server_reads_and_checks_each_stage_the_same_way() -> None:
    env = {f"VOICE_RELAY_{stage}": "compatible" for stage in ("STT", "LLM", "TTS")}
    assert settings.problems(env) == [
        f"VOICE_RELAY_{stage}_{field}: expected the {field.lower()} of your server"
        for stage in ("LLM", "STT", "TTS")
        for field in ("MODEL", "URL")
    ] + ["VOICE_RELAY_TTS_VOICE: expected a voice your server has"]

    for stage in ("STT", "LLM", "TTS"):
        env |= {
            f"VOICE_RELAY_{stage}_{field}": f"{stage} {field}" for field in ("URL", "KEY", "MODEL")
        }
    env["VOICE_RELAY_TTS_VOICE"] = "af_heart"
    assert settings.problems(env) == []
    loaded = settings.load(env)
    assert [(s.url, s.key, s.model) for s in (loaded.stt, loaded.llm, loaded.tts)] == [
        (f"{stage} URL", f"{stage} KEY", f"{stage} MODEL") for stage in ("STT", "LLM", "TTS")
    ]
