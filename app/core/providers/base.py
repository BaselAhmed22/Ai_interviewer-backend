# app/core/providers/base.py
"""
Provider interfaces for the voice pipeline. No STT/LLM/TTS vendor is
final yet — this layer exists so swapping one (OpenAI for Deepgram,
ElevenLabs for OpenAI's own TTS, ...) is a one-line .env change plus one
new provider class, never a change to VoiceAgent, the LiveKit worker, or
any other agent.

Each provider's only real job is `to_livekit()`: producing the actual
livekit-agents plugin instance (a `livekit.agents.stt.STT` / `llm.LLM` /
`tts.TTS` subclass) that AgentSession needs for real-time streaming in a
room. LiveKit's own plugin classes already implement correct, tested
audio streaming; providers here just wrap and select between them.
"""
from abc import ABC, abstractmethod
from typing import Any


class STTProvider(ABC):
    @abstractmethod
    def to_livekit(self) -> Any:
        """A livekit.agents.stt.STT instance, ready for AgentSession(stt=...)."""
        raise NotImplementedError


class LLMProvider(ABC):
    @abstractmethod
    def to_livekit(self) -> Any:
        """A livekit.agents.llm.LLM instance, ready for AgentSession(llm=...)."""
        raise NotImplementedError


class TTSProvider(ABC):
    @abstractmethod
    def to_livekit(self) -> Any:
        """A livekit.agents.tts.TTS instance, ready for AgentSession(tts=...)."""
        raise NotImplementedError
