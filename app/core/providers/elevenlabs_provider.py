# app/core/providers/elevenlabs_provider.py
"""Default TTS provider: ElevenLabs."""
from livekit.plugins import elevenlabs as livekit_elevenlabs

from app.core.providers.base import TTSProvider

# livekit-plugins-elevenlabs' own default voice.
_DEFAULT_VOICE_ID = "hpp4J3VqNfWAUOO0d1Us"


class ElevenLabsTTSProvider(TTSProvider):
    def __init__(self, voice_id: str = _DEFAULT_VOICE_ID) -> None:
        self._voice_id = voice_id

    def to_livekit(self) -> livekit_elevenlabs.TTS:
        return livekit_elevenlabs.TTS(voice_id=self._voice_id)
