"""Default STT + LLM provider: OpenAI."""
from typing import Optional

from livekit.plugins import openai as livekit_openai

from app.core.config import settings
from app.interviews.providers.base import LLMProvider, STTProvider


class OpenAISTTProvider(STTProvider):
    def to_livekit(self) -> livekit_openai.STT:
        return livekit_openai.STT()


class OpenAILLMProvider(LLMProvider):
    def __init__(self, model: Optional[str] = None) -> None:
        self._model = model or settings.LLM_MODEL

    def to_livekit(self) -> livekit_openai.LLM:
        return livekit_openai.LLM(model=self._model)
