from unittest import TestCase
from unittest.mock import Mock

from app.models.user import UserRole
from app.schemas.channel_model import AddChannelModelRequest
from app.services.auth_service import AuthService
from app.services.channel_model_service import ChannelModelService, _ChannelModelChange


class ChannelModelServiceTest(TestCase):
    def setUp(self) -> None:
        self.db = Mock()
        self.service = ChannelModelService(self.db, user_id="admin-id", actor_username="admin")

    def test_only_admin_has_model_write_permission(self) -> None:
        user = Mock(role=UserRole.ADMIN)
        self.assertTrue(AuthService.has_permission(user, "channel:model:write"))
        user.role = UserRole.OPERATOR
        self.assertFalse(AuthService.has_permission(user, "channel:model:write"))
        user.role = UserRole.CHANNEL_USER
        self.assertFalse(AuthService.has_permission(user, "channel:model:write"))

    def test_preview_skips_existing_model_and_keeps_model_order(self) -> None:
        self.service._list_channels = Mock(return_value=[
            {"id": 1, "name": "has-model", "models": "a, target , b", "status": 1},
            {"id": 2, "name": "needs-model", "models": "a,b", "status": 2},
        ])

        result = self.service.preview(AddChannelModelRequest(group="group", status="enabled", model="target"))

        self.assertEqual(result.total_channels, 2)
        self.assertEqual(result.already_present_count, 1)
        self.assertEqual(result.candidate_count, 1)
        self.assertEqual(result.candidates[0].channel_id, "2")
        self.assertEqual(result.candidates[0].updated_models, "a,b,target")

    def test_put_payload_only_contains_editable_fields(self) -> None:
        payload = self.service._make_put_payload(
            {
                "id": 42,
                "name": "channel",
                "models": "old",
                "setting": {"x": 1},
                "status": 1,
                "balance": 10,
                "channel_info": "readonly",
            },
            "old,new",
        )

        self.assertEqual(payload["models"], "old,new")
        self.assertEqual(payload["setting"], {"x": 1})
        self.assertNotIn("status", payload)
        self.assertNotIn("balance", payload)
        self.assertNotIn("channel_info", payload)

    def test_apply_records_partial_failure_without_stopping(self) -> None:
        changes = [
            _ChannelModelChange("1", "one", "a", "a,target", 1, {"id": 1, "models": "a"}),
            _ChannelModelChange("2", "two", "b", "b,target", 1, {"id": 2, "models": "b"}),
        ]
        self.service._changes = Mock(return_value=(changes, 2, 0))
        self.service._config = Mock(return_value=Mock())
        self.service._request = Mock(side_effect=[{"success": True}, RuntimeError("remote error")])

        result = self.service.apply(AddChannelModelRequest(group="group", status="enabled", model="target"))

        self.assertEqual(result.success_count, 1)
        self.assertEqual(result.failed_count, 1)
        self.assertEqual([item.status for item in result.results], ["SUCCESS", "FAILED"])
        self.db.add.assert_called_once()
        self.db.commit.assert_called_once()
