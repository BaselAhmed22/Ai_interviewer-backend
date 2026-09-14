# Four of the five interview-pipeline stages — DocumentAgent ->
# QuestionnaireAgent -> ControllerAgent -> VoiceAgent — run in sequence by
# InterviewPipelineManager (app/interviews/services/interview_pipeline.py).
# Stage 5, EvaluationAgent, lives in app/ai_evaluator/ instead: scoring an
# interview is a distinct concern from conducting one. See each module's
# docstring and app/interviews/schemas/agent_context.py for the
# input/output contract between stages.
