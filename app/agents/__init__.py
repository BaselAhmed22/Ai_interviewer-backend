# app/agents/ — the multi-agent interview pipeline.
#
# Five stages, run in sequence by InterviewPipelineManager
# (app/services/interview_pipeline.py):
#
#   DocumentAgent -> QuestionnaireAgent -> ControllerAgent -> VoiceAgent -> EvaluationAgent
#
# DocumentAgent, QuestionnaireAgent, ControllerAgent, and EvaluationAgent
# ship here as thin, working placeholders — each has a clear input/output
# contract (see their docstrings and app.schemas.agent_context) so the AI
# team can replace the placeholder logic without touching how the stages
# connect to each other or to the rest of the backend. VoiceAgent is the
# one stage that's already fully wired to real services (LiveKit,
# Deepgram/OpenAI, ElevenLabs) — it's what app/workers/livekit_agent.py
# actually runs.
