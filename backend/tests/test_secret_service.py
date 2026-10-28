import base64
import hashlib
import hmac
import os
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet

from app.core.config import Settings
from app.services.channel_upload_service import ChannelUploadService
from app.services.secret_service import SecretService, get_app_secret_key


class SecretServiceTest(TestCase):
    def test_dotenv_key_is_used_for_encryption_and_fingerprints(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ):
            os.environ.pop("APP_SECRET_KEY", None)
            dotenv = Path(directory) / ".env"
            key = "test-dotenv-stable-key"
            dotenv.write_text(f"APP_SECRET_KEY={key}\n")
            settings = Settings(_env_file=dotenv)
            self.assertNotIn(key, repr(settings))
            self.assertNotIn(key, settings.model_dump_json())
            with patch("app.services.secret_service.get_settings", return_value=settings):
                self.assertEqual(get_app_secret_key(require_stable=True), key)
                ciphertext = SecretService().encrypt_optional("management-token")
                legacy = Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))
                self.assertEqual(legacy.decrypt(ciphertext.encode()), b"management-token")
                self.assertEqual(SecretService().decrypt_optional(ciphertext), "management-token")
                service = ChannelUploadService(Mock(), "test-user")
                self.assertEqual(service.fingerprint("gemini_key", "test-credential"),
                                 hmac.new(key.encode(), b"gemini_key:test-credential", hashlib.sha256).hexdigest())

    def test_environment_key_has_precedence_and_preserves_exact_bytes(self):
        key = " existing-env-key-with-spaces "
        with patch.dict(os.environ, {"APP_SECRET_KEY": key}), \
                patch("app.services.secret_service.get_settings") as settings:
            self.assertEqual(get_app_secret_key(require_stable=True), key)
            old = Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))
            token = old.encrypt(b"existing-ciphertext").decode()
            self.assertEqual(SecretService().decrypt_optional(token), "existing-ciphertext")
            settings.assert_not_called()

    def test_empty_and_example_keys_are_rejected_without_leaking_values(self):
        for key in ("", "  ", "dev-only-change-me", "please-change-this-dev-secret",
                    "replace-with-a-long-random-secret"):
            with self.subTest(key=key), patch.dict(os.environ, {"APP_SECRET_KEY": key}), \
                    patch("app.services.secret_service.get_settings") as settings:
                with self.assertRaisesRegex(ValueError, "APP_SECRET_KEY"):
                    get_app_secret_key(require_stable=True)
                settings.assert_not_called()
