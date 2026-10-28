from unittest import TestCase
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models  # noqa: F401
from app.api.channel_config_api import update_channel_config
from app.db.session import Base
from app.models.user import UserRole
from app.schemas.channel_config import UpdateChannelConfigRequest
from app.services.auth_service import AuthService
from app.services.channel_push_service import ChannelPushService
from app.services.user_channel_config_service import UserChannelConfigService


class UserChannelConfigServiceTest(TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        auth = AuthService(self.db)
        self.admin = auth.create_user(
            username="admin",
            password="a-secure-password",
            role=UserRole.ADMIN,
        )
        self.operator = auth.create_user(
            username="operator",
            password="a-secure-password",
            role=UserRole.OPERATOR,
            actor=self.admin,
        )
        self.service = UserChannelConfigService(self.db)

    def tearDown(self) -> None:
        self.db.close()

    @staticmethod
    def request(*, token: str | None) -> UpdateChannelConfigRequest:
        return UpdateChannelConfigRequest.model_validate(
            {
                "baseUrl": "https://new-api.example.com/",
                "token": token,
            }
        )

    def test_legacy_token_is_encrypted_and_migrates_to_managed_target(self) -> None:
        stored = self.service.upsert(self.operator, self.request(token="channel-token"))

        self.assertNotEqual(stored.token_encrypted, "channel-token")
        self.assertEqual(stored.base_url, "https://new-api.example.com")
        from app.services.new_api_target_service import migrate_legacy_targets
        from app.models.channel_upload import NewApiTarget
        migrate_legacy_targets(self.db)
        target = self.db.get(NewApiTarget, self.operator.default_target_id)
        self.assertFalse(target.enabled)
        with self.assertRaises(ValueError): ChannelPushService(self.db, user_id=self.operator.id)._load_push_config()
        target.enabled = True; self.db.commit()
        self.operator.bound_target_id = self.operator.default_target_id
        self.db.commit()
        push_config = ChannelPushService(self.db, user_id=self.operator.id)._load_push_config()
        self.assertEqual(push_config.token, "channel-token")
        self.assertEqual(push_config.auth_scheme, "Bearer")

    def test_update_without_password_keeps_existing_password(self) -> None:
        stored = self.service.upsert(self.operator, self.request(token="channel-token"))
        encrypted = stored.token_encrypted

        updated = self.service.upsert(self.operator, self.request(token=None))

        self.assertEqual(updated.token_encrypted, encrypted)
        self.assertEqual(self.service.decrypt_token(updated), "channel-token")

    def test_each_user_has_an_independent_config(self) -> None:
        self.service.upsert(self.operator, self.request(token="operator-token"))
        admin_request = self.request(token="admin-token").model_copy(
            update={"base_url": "https://admin-api.example.com"}
        )
        self.service.upsert(self.admin, admin_request)

        self.assertEqual(self.service.decrypt_token(self.service.get(self.operator.id)), "operator-token")
        self.assertEqual(self.service.decrypt_token(self.service.get(self.admin.id)), "admin-token")

    def test_personal_connection_api_is_retired_without_network_or_save(self) -> None:
        with patch("app.services.channel_push_service.ChannelPushService.validate_config") as validate:
            with self.assertRaises(HTTPException) as raised:
                update_channel_config(self.request(token="channel-token"), user=self.operator, db=self.db)
        self.assertEqual(raised.exception.status_code, 410)
        validate.assert_not_called()
        self.assertIsNone(self.service.get(self.operator.id))

    def test_retired_api_does_not_overwrite_existing_connection(self) -> None:
        existing = self.service.upsert(self.operator, self.request(token="old-token"))
        with self.assertRaises(HTTPException) as raised:
            update_channel_config(self.request(token="new-token"), user=self.operator, db=self.db)
        self.assertEqual(raised.exception.status_code, 410)
        self.assertEqual(self.service.decrypt_token(existing), "old-token")
