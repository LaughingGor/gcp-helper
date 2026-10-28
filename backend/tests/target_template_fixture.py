import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from app.core.config import get_settings


def install_default_template(test_case):
    # Mock the transport, not validation, so saves exercise real response checks.
    request = patch("app.services.channel_push_service.ChannelPushService._perform_json_request",
                    return_value={"success": True, "data": {"items": [], "total": 0}})
    test_case.token_request = request.start()
    test_case.addCleanup(request.stop)
    directory = tempfile.TemporaryDirectory()
    test_case.addCleanup(directory.cleanup)
    root = Path(directory.name)
    (root / "default.json").write_text(json.dumps({"mode": "single", "channel": {
        "type": 41, "models": "template-model", "group": "template-group",
        "other": "us-central1", "priority": 17, "setting": {"force_format": True},
    }}))
    settings = get_settings()
    for name, value in (("new_api_channel_template_dir", root),
                        ("new_api_channel_default_template", "default"),
                        ("new_api_upload_vertex_template", ""),
                        ("new_api_upload_gemini_template", "")):
        patcher = patch.object(settings, name, value)
        patcher.start()
        test_case.addCleanup(patcher.stop)
