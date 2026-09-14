"""LLM provider: Gemma 4 31B, served through LiveKit Inference — billed to
the same LiveKit Cloud project as everything else here, no separate
provider API key needed."""
from livekit.agents import inference

from app.core.providers.base import LLMProvider


class GemmaLLMProvider(LLMProvider):
    def to_livekit(self) -> inference.LLM:
        return inference.LLM("google/gemma-4-31b-it")
