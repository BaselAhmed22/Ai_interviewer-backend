# app/agents/controller_agent.py
"""
ControllerAgent — pipeline stage 3.

Owns live interview flow control once the candidate is in the room: which
prepared question to ask next, and when the interview is complete. It
does not touch audio directly — that's VoiceAgent (LiveKit's AgentSession
handles STT/turn-taking/TTS internally); ControllerAgent decides *what*
the interviewer should say, VoiceAgent is *how* it gets said.

Placeholder implementation: walks the prepared question list strictly in
order, with no adaptive behavior. The AI team should replace this with
real logic — e.g. asking a follow-up based on the candidate's last
answer, skipping a question already answered elsewhere, or wrapping up
early — while keeping the same `AgentContext` in/out contract so
VoiceAgent's integration point (see app/agents/voice_agent.py) doesn't
need to change.
"""
from app.schemas.agent_context import AgentContext


class ControllerAgent:
    def next_instruction(self, context: AgentContext) -> str | None:
        """The instruction to hand VoiceAgent's LLM for its next turn, or
        None once every prepared question has been asked."""
        if context.current_question_index >= len(context.questions):
            return None
        return context.questions[context.current_question_index]

    def advance(self, context: AgentContext) -> None:
        context.current_question_index += 1

    def is_complete(self, context: AgentContext) -> bool:
        return context.current_question_index >= len(context.questions)
