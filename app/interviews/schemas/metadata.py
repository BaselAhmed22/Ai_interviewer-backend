from typing import Optional
from pydantic import Field
from app.core.schemas_base import CamelModel

class EyeContactData(CamelModel):
    is_looking_at_camera: bool
    confidence: float = Field(ge=0.0, le=1.0)

class PostureData(CamelModel):
    is_slouching: bool
    is_centered: bool

class AudioMetricsData(CamelModel):
    volume_level: float
    is_speaking: bool

class FrameMetadataPayload(CamelModel):
    session_id: str
    timestamp: float
    eye_contact: EyeContactData
    posture: PostureData
    audio: Optional[AudioMetricsData] = None

class AlertMessage(CamelModel):
    session_id: str
    alert_type: str
    message: str
    timestamp: float