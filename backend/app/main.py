import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.gcp_env_api import router as gcp_env_router
from app.api.project_replacement_api import router as project_replacement_router
from app.api.preset_task_api import router as preset_task_router
from app.api.auth_api import admin_router, router as auth_router
from app.api.channel_config_api import router as channel_config_router
from app.api.channel_model_api import router as channel_model_router
from app.api.channel_statistics_api import router as channel_statistics_router
from app.api.user_settings_api import router as user_settings_router
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.init_db import init_db
from app.services.gcp_env_service import recover_interrupted_login_sessions
from app.api.channel_upload_api import router as channel_upload_router
from app.services.channel_upload_service import recover_channel_uploads, start_usage_worker, stop_usage_worker

setup_logging()
settings = get_settings()
logger = logging.getLogger(__name__)

app = FastAPI(title=settings.app_name)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    logger.info("service starting app=%s api_prefix=%s", settings.app_name, settings.api_prefix)
    init_db()
    from app.db.session import SessionLocal
    from app.services.new_api_target_service import migrate_legacy_targets
    with SessionLocal() as db:
        migrate_legacy_targets(db)
    recover_interrupted_login_sessions()
    from app.services.preset_task_service import recover_interrupted_preset_tasks
    recover_interrupted_preset_tasks()
    recover_channel_uploads()
    from app.services.project_replacement_service import recover_project_replacements
    recover_project_replacements()
    start_usage_worker()
    logger.info("database initialized")


@app.on_event("shutdown")
def on_shutdown() -> None:
    stop_usage_worker()


@app.get("/health")
def health():
    return {"status": "ok"}


app.include_router(gcp_env_router, prefix=settings.api_prefix)
app.include_router(project_replacement_router, prefix=settings.api_prefix)
app.include_router(preset_task_router, prefix=settings.api_prefix)
app.include_router(auth_router, prefix=settings.api_prefix)
app.include_router(admin_router, prefix=settings.api_prefix)
app.include_router(channel_config_router, prefix=settings.api_prefix)
app.include_router(channel_model_router, prefix=settings.api_prefix)
app.include_router(channel_statistics_router, prefix=settings.api_prefix)
app.include_router(user_settings_router, prefix=settings.api_prefix)
app.include_router(channel_upload_router, prefix=settings.api_prefix)
