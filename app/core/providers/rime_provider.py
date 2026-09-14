"""TTS provider: Rime, Coda model."""
from livekit.plugins import rime

from app.core.providers.base import TTSProvider


class RimeTTSProvider(TTSProvider):
    def __init__(self, speaker: str = "celeste") -> None:
        self._speaker = speaker

    def to_livekit(self) -> rime.TTS:
        return rime.TTS(model="coda", speaker=self._speaker)
