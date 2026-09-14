# Five-stage interview pipeline, run in sequence by InterviewPipelineManager
# (app/services/interview_pipeline.py):
#
#   DocumentAgent -> QuestionnaireAgent -> ControllerAgent -> VoiceAgent -> EvaluationAgent
#
# See each module's docstring and app.schemas.agent_context for the
# input/output contract between stages.
