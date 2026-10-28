import argparse
import getpass

from app.db.init_db import init_db
from app.db.session import SessionLocal
from app.models.user import User, UserRole
from app.services.auth_service import AuthService


def create_admin(username: str) -> None:
    init_db()
    password = getpass.getpass("Admin password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match")
    db = SessionLocal()
    try:
        try:
            user = AuthService(db).create_user(username=username, password=password, role=UserRole.ADMIN)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print(f"Created administrator: {user.username}")
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="GCP Helper administration")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_admin_parser = subparsers.add_parser("create-admin")
    create_admin_parser.add_argument("--username", default="admin")
    args = parser.parse_args()
    if args.command == "create-admin":
        create_admin(args.username)


if __name__ == "__main__":
    main()
