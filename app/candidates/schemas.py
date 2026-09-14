from datetime import datetime
from pydantic import Field
from app.core.schemas_base import CamelModel, NoControlCharsMixin

class JobDescriptionRequest(NoControlCharsMixin, CamelModel):
    job_title: str = Field(min_length=1, max_length=255)
    description_text: str = Field(min_length=1, max_length=20000)

class InterviewPreferenceRequest(NoControlCharsMixin, CamelModel):
    company_name: str = Field(min_length=1, max_length=255)
    job_title: str = Field(min_length=1, max_length=255)
    language: str = Field(default="en", min_length=2, max_length=20)
    interview_date: datetime

class CvUploadResponse(CamelModel):
    message: str
    profile_id: str
    file_path: str

class JobDescriptionResponse(CamelModel):
    message: str
    job_id: str

class InterviewPreferenceResponse(CamelModel):
    message: str
    preference_id: str