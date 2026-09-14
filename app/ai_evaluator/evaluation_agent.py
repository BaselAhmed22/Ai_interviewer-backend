"""
EvaluationAgent — pipeline stage 5.

Produces the final interview report once a session ends. This is what
app.workers.tasks.generate_final_interview_report (the Celery task,
dispatched automatically by POST /sessions/end/{id} and explicitly by
POST /api/v1/interviews/evaluate) delegates to — the task owns
retries/durability/writing to the InterviewReport table, this class
holds the actual scoring logic.

Placeholder implementation: fixed sample scores. Replace `evaluate()`
with real analysis of the interview transcript/audio, keeping the
returned dict's keys matching app.interviews.schemas.report.ReportBase.
"""


class EvaluationAgent:
    def evaluate(self, session_id: str) -> dict:
        return {
            "overall_score": 88.5,
            "eye_contact_score": 90.0,
            "posture_score": 85.0,
            "speech_clarity_score": 90.5,
            "feedback_summary": "Great overall performance with stable body posture and strong eye contact.",
            "is_placeholder": True,
        }
