"""
Picks a concrete provider class based on settings.STT_PROVIDER /
LLM_PROVIDER / TTS_PROVIDER (.env-driven — see app/core/config.py)
instead of anything hardcoded. Add a new vendor by adding one entry to
the relevant registry below plus one new provider class implementing
STTProvider/LLMProvider/TTSProvider (app/core/providers/base.py) —
nothing else in the codebase (VoiceAgent, the LiveKit worker, other
agents) needs to change.
"""
from app.core.config import settings
from app.core.providers.base import LLMProvider, STTProvider, TTSProvider
from app.core.providers.deepgram_provider import DeepgramSTTProvider
from app.core.providers.elevenlabs_provider import ElevenLabsTTSProvider
from app.core.providers.gemma_provider import GemmaLLMProvider
from app.core.providers.openai_provider import OpenAILLMProvider, OpenAISTTProvider
from app.core.providers.rime_provider import RimeTTSProvider

_STT_PROVIDERS: dict[str, type[STTProvider]] = {
    "openai": OpenAISTTProvider,
    "deepgram": DeepgramSTTProvider,
}

_LLM_PROVIDERS: dict[str, type[LLMProvider]] = {
    "openai": OpenAILLMProvider,
    "gemma": GemmaLLMProvider,
}

_TTS_PROVIDERS: dict[str, type[TTSProvider]] = {
    "elevenlabs": ElevenLabsTTSProvider,
    "rime": RimeTTSProvider,
}


def get_stt_provider() -> STTProvider:
    provider_cls = _STT_PROVIDERS.get(settings.STT_PROVIDER)
    if provider_cls is None:
        raise ValueError(
            f"Unknown STT_PROVIDER '{settings.STT_PROVIDER}'. Available: {', '.join(_STT_PROVIDERS)}"
        )
    return provider_cls()


def get_llm_provider() -> LLMProvider:
    provider_cls = _LLM_PROVIDERS.get(settings.LLM_PROVIDER)
    if provider_cls is None:
        raise ValueError(
            f"Unknown LLM_PROVIDER '{settings.LLM_PROVIDER}'. Available: {', '.join(_LLM_PROVIDERS)}"
        )
    return provider_cls()


def get_tts_provider() -> TTSProvider:
    provider_cls = _TTS_PROVIDERS.get(settings.TTS_PROVIDER)
    if provider_cls is None:
        raise ValueError(
            f"Unknown TTS_PROVIDER '{settings.TTS_PROVIDER}'. Available: {', '.join(_TTS_PROVIDERS)}"
        )
    return provider_cls()
