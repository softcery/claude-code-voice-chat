from voice_relay import engines


def test_variant_takes_the_voice_of_its_own_provider_only():
    other = {"VOICE_RELAY_TTS": "elevenlabs", "VOICE_RELAY_TTS_VOICE": "21m00Tcm4TlvDq8ikWAM"}
    own = {"VOICE_RELAY_TTS": "piper", "VOICE_RELAY_TTS_VOICE": "de_DE-thorsten-medium"}

    assert engines.variant("piper", other) == engines.DEFAULTS["piper"]
    assert engines.variant("piper", own) == "de_DE-thorsten-medium"
    assert engines.variant("moonshine", {"VOICE_RELAY_STT_LANGUAGE": "de-DE"}) == "de"


def test_remove_keeps_the_extras_of_the_engines_left(monkeypatch):
    installed = {"whisper", "piper", "kokoro"}
    synced: list[tuple[str, ...]] = []
    monkeypatch.setattr(engines, "_packages", lambda provider: provider in installed)
    monkeypatch.setattr(engines, "_models", lambda provider: None)
    monkeypatch.setattr(engines, "_sync", lambda *flags: synced.append(flags))

    engines.remove("piper")

    assert synced == [("--extra", "whisper", "--extra", "kokoro")]
