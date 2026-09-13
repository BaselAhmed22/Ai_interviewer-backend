# app/core/providers/ — abstract STT/LLM/TTS provider interfaces and their
# concrete implementations. No vendor is final yet (the AI team hasn't
# picked STT/LLM/TTS models), so nothing in the voice pipeline should
# hardcode one. See base.py for the interfaces, factory.py for how a
# concrete provider gets selected (STT_PROVIDER/LLM_PROVIDER/TTS_PROVIDER
# in .env), and openai_provider.py/elevenlabs_provider.py for the
# default implementations that keep the pipeline runnable today.
