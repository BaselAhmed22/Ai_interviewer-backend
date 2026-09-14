"""STT provider: Deepgram Nova-3, monolingual English."""
from livekit.plugins import deepgram

from app.interviews.providers.base import STTProvider


class DeepgramSTTProvider(STTProvider):
    def to_livekit(self) -> deepgram.STT:
        return deepgram.STT(model="nova-3", language="en-US")
