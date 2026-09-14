from fastapi import APIRouter
from app.auth.api import router as auth_router
from app.candidates.api import router as candidates_router
from app.interviews.api.sessions import router as sessions_router
from app.interviews.api.pipeline import router as pipeline_router
from app.interviews.api.legacy import router as legacy_interview_router
from app.interviews.api.analytics_ws import router as analytics_ws_router
from app.admin.api import router as admin_router

api_router = APIRouter()
api_router.include_router(auth_router, prefix="/auth", tags=["Authentication"])
api_router.include_router(analytics_ws_router, tags=["Real-time Analytics"])
api_router.include_router(sessions_router, prefix="/sessions", tags=["Sessions"])
api_router.include_router(candidates_router, prefix="/candidates", tags=["Candidates"])
api_router.include_router(legacy_interview_router, prefix="/interview", tags=["Interview"])
api_router.include_router(pipeline_router, prefix="/interviews", tags=["Interview Pipeline"])
api_router.include_router(admin_router, prefix="/admin", tags=["Admin"])
