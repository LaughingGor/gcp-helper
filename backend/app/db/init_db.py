from app.db.session import Base, engine
from app import models  # noqa: F401 - ensure models are imported
from sqlalchemy import text


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _ensure_channel_upload_columns()
    _ensure_postgresql_user_roles()
    _migrate_removed_user_roles()
    _ensure_sqlite_preset_task_columns()
    _ensure_sqlite_environment_columns()
    _ensure_sqlite_environment_billing_columns()
    _ensure_sqlite_user_columns()
    _ensure_sqlite_user_channel_config_columns()
    _ensure_sqlite_date_indexes()
    _ensure_unified_workflow_columns()
    _ensure_target_stats_group()


def _ensure_target_stats_group() -> None:
    from sqlalchemy import inspect
    existing = {column["name"] for column in inspect(engine).get_columns("new_api_targets")}
    with engine.begin() as conn:
        if "stats_group" not in existing:
            conn.execute(text("ALTER TABLE new_api_targets ADD COLUMN stats_group VARCHAR(128)"))
        # Preserve each existing backend's statistics scope on upgrade.
        conn.execute(text('UPDATE new_api_targets SET stats_group = "group" WHERE stats_group IS NULL'))


def _ensure_unified_workflow_columns() -> None:
    from sqlalchemy import inspect
    additions = {
        "users": {"default_target_id": "VARCHAR(36) REFERENCES new_api_targets(id)",
                  "bound_target_id": "VARCHAR(36) REFERENCES new_api_targets(id)"},
        "gcp_preset_tasks": {"requested_vm_zone": "VARCHAR(128)", "credential_upload_id": "VARCHAR(36) REFERENCES channel_uploads(id)",
                            "force_new_project": "BOOLEAN NOT NULL DEFAULT false", "resource_plan_encrypted": "TEXT"},
        "channel_uploads": {"origin": "VARCHAR(32) NOT NULL DEFAULT 'UPLOAD'", "origin_env_id": "VARCHAR(36)",
                            "origin_task_id": "VARCHAR(36)", "proxy_encrypted": "TEXT"},
        "channel_push_records": {"actor_user_id": "VARCHAR(36) REFERENCES users(id)",
                                 "target_id": "VARCHAR(36) REFERENCES new_api_targets(id)",
                                 "deployment_id": "VARCHAR(36) REFERENCES channel_uploads(id)"},
    }
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, columns in additions.items():
            existing = {c["name"] for c in inspector.get_columns(table)}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_channel_uploads_origin_env_id ON channel_uploads (origin_env_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_channel_push_records_deployment_id ON channel_push_records (deployment_id)"))


def _ensure_channel_upload_columns() -> None:
    # Additive migration preserves existing uploads and their remote usage history.
    from sqlalchemy import inspect
    existing = {column["name"] for column in inspect(engine).get_columns("channel_uploads")}
    with engine.begin() as conn:
        for column, reference in (("source_upload_id", "channel_uploads"), ("target_id", "new_api_targets")):
            if column not in existing:
                conn.execute(text(f"ALTER TABLE channel_uploads ADD COLUMN {column} VARCHAR(36) REFERENCES {reference}(id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_channel_uploads_source_upload_id ON channel_uploads (source_upload_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_channel_uploads_target_id ON channel_uploads (target_id)"))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_channel_uploads_source_target ON channel_uploads (source_upload_id, target_id)"))


def _ensure_postgresql_user_roles() -> None:
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text("ALTER TYPE userrole ADD VALUE IF NOT EXISTS 'CHANNEL_USER'"))


def _migrate_removed_user_roles() -> None:
    # Use raw SQL before any ORM user reads: removed enum values cannot be loaded.
    # Disable accounts and revoke sessions before assigning a more capable role.
    with engine.begin() as conn:
        removed_users = "SELECT id FROM users WHERE CAST(role AS VARCHAR) = 'VIEWER'"
        conn.execute(text(
            "UPDATE user_auth_sessions SET revoked_at = CURRENT_TIMESTAMP "
            f"WHERE revoked_at IS NULL AND user_id IN ({removed_users})"
        ))
        conn.execute(text(
            "UPDATE users SET role = 'OPERATOR', status = 'DISABLED', updated_at = CURRENT_TIMESTAMP "
            "WHERE CAST(role AS VARCHAR) = 'VIEWER'"
        ))
        if engine.dialect.name == "postgresql":
            labels = conn.execute(text(
                "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid "
                "JOIN pg_namespace n ON t.typnamespace = n.oid "
                "WHERE t.typname = 'userrole' AND n.nspname = current_schema()"
            )).scalars().all()
            if "VIEWER" in labels:
                # PostgreSQL cannot drop an enum label in place.
                conn.execute(text("ALTER TYPE userrole RENAME TO userrole_legacy"))
                conn.execute(text("CREATE TYPE userrole AS ENUM ('ADMIN', 'OPERATOR', 'CHANNEL_USER')"))
                conn.execute(text("ALTER TABLE users ALTER COLUMN role TYPE userrole USING role::text::userrole"))
                conn.execute(text("DROP TYPE userrole_legacy"))


def _ensure_sqlite_preset_task_columns() -> None:
    if engine.dialect.name != "sqlite":
        return

    required_columns = {
        "vm_name": "VARCHAR(255)",
        "vm_zone": "VARCHAR(128)",
        "vm_external_ip": "VARCHAR(64)",
        "firewall_rule_name": "VARCHAR(255)",
        "socks5_port": "VARCHAR(16)",
        "socks5_url": "TEXT",
    }
    with engine.begin() as conn:
        existing = {row[1] for row in conn.execute(text("PRAGMA table_info(gcp_preset_tasks)"))}
        for name, column_type in required_columns.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE gcp_preset_tasks ADD COLUMN {name} {column_type}"))


def _ensure_sqlite_environment_columns() -> None:
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        existing = {row[1] for row in conn.execute(text("PRAGMA table_info(gcp_environments)"))}
        if "owner_user_id" not in existing:
            conn.execute(text("ALTER TABLE gcp_environments ADD COLUMN owner_user_id VARCHAR(36)"))
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_gcp_environments_owner_user_id "
                "ON gcp_environments (owner_user_id)"
            )
        )


def _ensure_sqlite_environment_billing_columns() -> None:
    if engine.dialect.name != "sqlite":
        return
    required_columns = {
        "billing_account_id": "VARCHAR(32)",
        "billing_account_display_name": "VARCHAR(255)",
        "billing_account_selected_at": "DATETIME",
        "billing_account_selected_by": "VARCHAR(128)",
    }
    with engine.begin() as conn:
        existing = {row[1] for row in conn.execute(text("PRAGMA table_info(gcp_environments)"))}
        for name, column_type in required_columns.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE gcp_environments ADD COLUMN {name} {column_type}"))


def _ensure_sqlite_user_columns() -> None:
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        existing = {row[1] for row in conn.execute(text("PRAGMA table_info(users)"))}
        if "compute_vm_zone" not in existing:
            conn.execute(text("ALTER TABLE users ADD COLUMN compute_vm_zone VARCHAR(128)"))


def _ensure_sqlite_user_channel_config_columns() -> None:
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        existing = {row[1] for row in conn.execute(text("PRAGMA table_info(user_channel_configs)"))}
        if "token_encrypted" not in existing:
            conn.execute(text("ALTER TABLE user_channel_configs ADD COLUMN token_encrypted TEXT"))


def _ensure_sqlite_date_indexes() -> None:
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_gcp_env_owner_created "
                "ON gcp_environments (owner_user_id, created_at)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_preset_task_created "
                "ON gcp_preset_tasks (created_at)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_preset_task_env_created "
                "ON gcp_preset_tasks (env_id, created_at)"
            )
        )
