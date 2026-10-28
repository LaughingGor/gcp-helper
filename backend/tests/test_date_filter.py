from datetime import date, datetime
from unittest import TestCase

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models  # noqa: F401
from app.core.date_filter import beijing_date_span_utc_range, beijing_day_utc_range, beijing_today
from app.db.session import Base
from app.models.gcp_environment import GcpEnvironment, GcpEnvironmentStatus
from app.services.gcp_env_service import GcpEnvService


class BeijingDateFilterTest(TestCase):
    def test_beijing_today_returns_a_date(self) -> None:
        self.assertIsInstance(beijing_today(), date)

    def test_beijing_day_is_converted_to_utc_bounds(self) -> None:
        start, end = beijing_day_utc_range(date(2026, 9, 7))

        self.assertEqual(start, datetime(2026, 9, 6, 16, 0, 0))
        self.assertEqual(end, datetime(2026, 9, 7, 16, 0, 0))

    def test_beijing_date_span_includes_the_whole_end_date(self) -> None:
        start, end = beijing_date_span_utc_range(date(2026, 9, 7), date(2026, 9, 9))

        self.assertEqual(start, datetime(2026, 9, 6, 16, 0, 0))
        self.assertEqual(end, datetime(2026, 9, 9, 16, 0, 0))

    def test_beijing_date_span_rejects_reversed_range(self) -> None:
        with self.assertRaisesRegex(ValueError, "开始日期"):
            beijing_date_span_utc_range(date(2026, 9, 9), date(2026, 9, 7))

    def test_environment_filter_uses_left_closed_right_open_range(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            timestamps = [
                ("before", datetime(2026, 9, 6, 15, 59, 59)),
                ("start", datetime(2026, 9, 6, 16, 0, 0)),
                ("end-minus", datetime(2026, 9, 7, 15, 59, 59)),
                ("end", datetime(2026, 9, 7, 16, 0, 0)),
            ]
            for index, (name, created_at) in enumerate(timestamps):
                db.add(
                    GcpEnvironment(
                        env_name=name,
                        google_account_hint=f"user{index}@example.com",
                        config_name=f"config-{index}",
                        config_dir=f"/tmp/config-{index}",
                        status=GcpEnvironmentStatus.CREATED,
                        created_at=created_at,
                    )
                )
            db.commit()
            start, end = beijing_day_utc_range(date(2026, 9, 7))

            environments = GcpEnvService(db).list_envs(created_from=start, created_to=end)

            self.assertEqual([env.env_name for env in environments], ["end-minus", "start"])
        finally:
            db.close()
