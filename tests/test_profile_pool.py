import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from src.api.service import Gateway, Job
from src.contract import Result
from src.worker.browser import Browser, ProfilePoolExhaustedError


class ProfilePoolTest(unittest.TestCase):
    def test_blocked_profile_is_preserved_and_next_profile_is_selected(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ,
            {"AIFARM_PROFILE_ROOT": root, "AIFARM_PROFILE_COUNT": "3"},
        ):
            os.environ.pop("AIFARM_PROFILE_PATH", None)
            browser = Browser()

            blocked, next_profile = browser.burn_and_rotate()

            self.assertEqual((blocked, next_profile), ("user1", "user2"))
            self.assertTrue((Path(root) / "user1" / ".needs_attention").is_file())
            self.assertTrue((Path(root) / "user1").is_dir())

    def test_cooled_down_profile_returns_to_rotation(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ,
            {"AIFARM_PROFILE_ROOT": root, "AIFARM_PROFILE_COUNT": "2"},
        ):
            os.environ.pop("AIFARM_PROFILE_PATH", None)
            for number, age in ((1, timedelta(hours=9)), (2, timedelta(minutes=5))):
                profile = Path(root) / f"user{number}"
                profile.mkdir()
                (profile / ".needs_attention").write_text(
                    (datetime.now(UTC) - age).isoformat(), encoding="utf-8"
                )

            browser = Browser()

            # user2 ещё остывает, user1 отсидел свой COOLDOWN и снова в строю
            self.assertEqual(browser.profile_id, "user1")
            self.assertFalse((Path(root) / "user1" / ".needs_attention").exists())
            self.assertTrue((Path(root) / "user2" / ".needs_attention").is_file())

    def test_all_blocked_pool_falls_back_to_longest_rested(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ,
            {"AIFARM_PROFILE_ROOT": root, "AIFARM_PROFILE_COUNT": "3"},
        ):
            os.environ.pop("AIFARM_PROFILE_PATH", None)
            for number, age in ((1, timedelta(minutes=10)), (2, timedelta(minutes=40)),
                                (3, timedelta(minutes=25))):
                profile = Path(root) / f"user{number}"
                profile.mkdir()
                (profile / ".needs_attention").write_text(
                    (datetime.now(UTC) - age).isoformat(), encoding="utf-8"
                )

            browser = Browser()

            # свободных нет — берём самый отдохнувший, а не «человека к клавиатуре»
            self.assertEqual(browser.profile_id, "user2")
            for number in (1, 3):
                self.assertTrue((Path(root) / f"user{number}" / ".needs_attention").is_file())

    def test_manual_marker_keeps_profile_out_of_the_pool(self):
        """Пустой маркер = поставлен руками: профиль ждёт ручного прогрева."""
        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ,
            {"AIFARM_PROFILE_ROOT": root, "AIFARM_PROFILE_COUNT": "2"},
        ):
            os.environ.pop("AIFARM_PROFILE_PATH", None)
            for number in (1, 2):
                (Path(root) / f"user{number}").mkdir()
            (Path(root) / "user1" / ".needs_attention").touch()

            browser = Browser()

            self.assertEqual(browser.profile_id, "user2")

    def test_exhausted_pool_requires_manual_action(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ,
            {"AIFARM_PROFILE_ROOT": root, "AIFARM_PROFILE_COUNT": "2"},
        ):
            os.environ.pop("AIFARM_PROFILE_PATH", None)
            for number in (1, 2):
                profile = Path(root) / f"user{number}"
                profile.mkdir()
                (profile / ".needs_attention").touch()

            browser = Browser()

            with self.assertRaises(ProfilePoolExhaustedError):
                browser._ensure_browser()


class GatewayProfileStatusTest(unittest.TestCase):
    def test_profile_change_is_exposed_to_mcp_layer(self):
        gateway = Gateway()
        gateway.jobs["job"] = Job(user_session_id="user", tab_id=None)

        gateway._record_result(
            "job",
            Result(
                error="profile_blocked",
                sessions=["worker:tab"],
                profile_id="user1",
                next_profile_id="user2",
            ),
        )

        self.assertEqual(
            gateway.get_job("user", "job"),
            {
                "status": "blocked",
                "tab_id": None,
                "closed_tab_ids": ["worker:tab"],
                "profile_id": "user1",
                "next_profile_id": "user2",
            },
        )


if __name__ == "__main__":
    unittest.main()
