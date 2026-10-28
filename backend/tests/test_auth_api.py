from unittest import TestCase

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app import models  # noqa: F401
from app.core.auth import AuthContext, _verify_csrf
from app.core.config import get_settings
from app.db.session import Base
from app.models.user import UserRole
from app.services.auth_service import AuthService


class AuthApiTest(TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        service = AuthService(self.db)
        user = service.create_user(
            username="admin",
            password="a-secure-password",
            role=UserRole.ADMIN,
        )
        created = service.create_session(user, ip_address="127.0.0.1", user_agent="test")
        self.context = AuthContext(user, created.session)
        self.csrf_token = created.raw_csrf_token

    def tearDown(self) -> None:
        self.db.close()

    def request(self, *, cookie_token: str, header_token: str) -> Request:
        settings = get_settings()
        headers = [
            (b"cookie", f"{settings.auth_csrf_cookie_name}={cookie_token}".encode()),
            (b"x-csrf-token", header_token.encode()),
        ]
        return Request({"type": "http", "method": "POST", "path": "/", "headers": headers})

    def test_valid_csrf_token_is_accepted(self) -> None:
        _verify_csrf(
            self.request(cookie_token=self.csrf_token, header_token=self.csrf_token),
            self.context,
        )

    def test_mismatched_csrf_token_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            _verify_csrf(
                self.request(cookie_token=self.csrf_token, header_token="wrong-token"),
                self.context,
            )
        self.assertEqual(raised.exception.status_code, 403)
