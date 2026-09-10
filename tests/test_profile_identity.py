"""Личность профиля: постоянное устройство и ротация по нагрузке."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.worker.browser import (
    ANSWER_ERROR,
    AnswerRefusedError,
    DEVICE_FILE,
    VOLATILE_CONFIG_KEYS,
    Browser,
    _clamp_window_position,
    _generate_device,
    _google_url,
)

FAKE_DEVICE = {"navigator.platform": "Linux x86_64", "canvas:seed": 42}


class FakePage:
    url = "about:blank"

    def bring_to_front(self):
        pass


class FakeContext:
    def __init__(self):
        self.pages = [FakePage()]


class PoolTestCase(unittest.TestCase):
    """Browser на временном пуле из трёх профилей."""

    def make_browser(self, max_queries: int = 12) -> Browser:
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        # норма читается из модуля, а не из env: env разбирается при импорте
        self.enterContext(patch("src.worker.browser.PROFILE_MAX_QUERIES", max_queries))
        with patch.dict(
            os.environ,
            {"AIFARM_PROFILE_ROOT": root.name, "AIFARM_PROFILE_COUNT": "3"},
        ):
            os.environ.pop("AIFARM_PROFILE_PATH", None)
            return Browser()


class DeviceTest(PoolTestCase):
    def test_google_url_uses_the_browser_language(self):
        self.assertEqual(_google_url("fr-FR"), "https://www.google.com/?hl=fr")
        self.assertEqual(_google_url(None), "https://www.google.com")

    def test_device_is_generated_once_and_then_reused(self):
        browser = self.make_browser()
        with patch("src.worker.browser._generate_device", return_value=FAKE_DEVICE) as gen:
            first = browser._device_config()
            second = browser._device_config()

        self.assertEqual(first, second)
        self.assertEqual(gen.call_count, 1, "устройство должно генерироваться один раз")
        saved = json.loads((self.root / "user1" / DEVICE_FILE).read_text())
        self.assertEqual(saved, FAKE_DEVICE)

    def test_each_profile_gets_its_own_device(self):
        browser = self.make_browser()
        with patch("src.worker.browser._generate_device", side_effect=[{"a": 1}, {"a": 2}]):
            browser._device_config()
            browser.burn_and_rotate()
            self.assertEqual(browser.profile_id, "user2")
            self.assertEqual(browser._device_config(), {"a": 2})

        self.assertEqual(json.loads((self.root / "user1" / DEVICE_FILE).read_text()), {"a": 1})

    def test_saved_device_reaches_launch_config(self):
        browser = self.make_browser()
        with patch("src.worker.browser._generate_device", return_value=FAKE_DEVICE):
            self.assertEqual(browser._launch_config()["config"], FAKE_DEVICE)

    def test_fixed_locale_reaches_camoufox_without_entering_device_identity(self):
        browser = self.make_browser()
        with patch("src.worker.browser.BROWSER_LOCALE", "fr-FR"), \
             patch("src.worker.browser._generate_device", return_value=FAKE_DEVICE):
            config = browser._launch_config()

        self.assertEqual(config["locale"], "fr-FR")
        self.assertNotIn("locale:language", config["config"])

    def test_window_position_is_kept_inside_the_screen(self):
        """Camoufox позицию окна не проверяет: BrowserForge моделирует второй монитор."""
        device = _clamp_window_position({
            "screen.width": 1680, "screen.availWidth": 1680,
            "screen.height": 1050, "screen.availHeight": 1002,
            "window.outerWidth": 1680, "window.outerHeight": 1002,
            "window.screenX": 3432, "window.screenY": -1,
        })

        self.assertEqual(device["window.screenX"], 0)
        self.assertEqual(device["window.screenY"], 0)

    def test_window_position_inside_the_screen_is_left_alone(self):
        device = _clamp_window_position({
            "screen.width": 1920, "screen.availWidth": 1920,
            "screen.height": 1080, "screen.availHeight": 1040,
            "window.outerWidth": 1200, "window.outerHeight": 800,
            "window.screenX": 300, "window.screenY": 100,
        })

        self.assertEqual((device["window.screenX"], device["window.screenY"]), (300, 100))

    def test_saved_device_with_offscreen_window_is_repaired_on_read(self):
        browser = self.make_browser()
        broken = {"screen.width": 1680, "screen.availWidth": 1680,
                  "screen.height": 1050, "screen.availHeight": 1002,
                  "window.outerWidth": 1680, "window.outerHeight": 1002,
                  "window.screenX": 3432, "window.screenY": -1}
        path = self.root / "user1" / DEVICE_FILE
        path.write_text(json.dumps(broken))

        device = browser._device_config()

        self.assertEqual(device["window.screenX"], 0)
        self.assertEqual(json.loads(path.read_text())["window.screenX"], 0,
                         "починка должна дойти до диска, а не жить в памяти")

    def test_generated_device_puts_the_window_on_screen(self):
        device = _generate_device()

        self.assertGreaterEqual(device["window.screenX"], 0)
        self.assertLessEqual(
            device["window.screenX"] + device["window.outerWidth"], device["screen.width"]
        )

    def test_generated_device_is_hardware_without_geography(self):
        """Железо сохраняем, географию — нет: её считает geoip по текущему IP."""
        device = _generate_device()

        self.assertIn("navigator.userAgent", device)
        self.assertIn("canvas:seed", device, "seed холста — главный признак машины")
        self.assertIn("screen.width", device)
        volatile = [key for key in device if key.startswith(VOLATILE_CONFIG_KEYS)]
        self.assertEqual(volatile, [], "география и наши настройки в устройство не попадают")


class TimeoutCeilingTest(PoolTestCase):
    def test_context_gets_ceilings_on_every_playwright_call(self):
        """Часть вызовов Playwright не имеет таймаута вовсе — ставим общий потолок."""
        from src.worker.browser import EL_TIMEOUT, NAV_TIMEOUT

        browser = self.make_browser()
        with patch("src.worker.browser.Camoufox") as camoufox, \
             patch("src.worker.browser._generate_device", return_value=FAKE_DEVICE):
            context = camoufox.return_value.start.return_value

            browser._ensure_browser()

        context.set_default_timeout.assert_called_once_with(EL_TIMEOUT)
        context.set_default_navigation_timeout.assert_called_once_with(NAV_TIMEOUT)


class LoadRotationTest(PoolTestCase):
    def test_budget_stays_within_declared_band(self):
        for _ in range(20):
            self.assertIn(self.make_browser(12)._profile_budget, (10, 11, 12))

    def test_spent_profile_gives_way_to_the_next_one(self):
        browser = self.make_browser()
        browser._profile_budget = 0

        browser._rotate_by_load()

        self.assertEqual(browser.profile_id, "user2")
        self.assertGreater(browser._profile_budget, 0, "новый профиль получает свою норму")
        self.assertFalse(
            (self.root / "user1" / ".needs_attention").exists(),
            "смена по нагрузке — не блокировка, маркер ставить нельзя",
        )

    def test_profile_with_budget_left_keeps_working(self):
        browser = self.make_browser()
        browser._profile_budget = 1

        browser._rotate_by_load()

        self.assertEqual(browser.profile_id, "user1")

    def test_zero_disables_load_rotation(self):
        browser = self.make_browser(max_queries=0)
        browser._profile_budget = 0

        browser._rotate_by_load()

        self.assertEqual(browser.profile_id, "user1")

    def test_every_question_spends_the_budget_and_search_rotates(self):
        browser = self.make_browser(max_queries=3)
        browser._profile_budget = 2
        with patch.object(Browser, "_ensure_browser", return_value=FakeContext()), \
             patch.object(Browser, "_open_ai_mode"):
            session = browser.new_search_session("u", "первый вопрос")
            self.assertEqual(browser._profile_budget, 1)

            # follow-up тратит норму, но профиль не меняет: вкладка живёт здесь
            with patch.object(Browser, "_type_query"), \
                 patch.object(Browser, "_read_pause_seconds", return_value=0.0), \
                 patch.object(Browser, "_session") as session_of:
                session_of.return_value.page.locator.return_value.count.return_value = 0
                browser.ask_followup("u", session, "уточнение")
            self.assertEqual(browser._profile_budget, 0)
            self.assertEqual(browser.profile_id, "user1")

            browser.new_search_session("u", "новый разговор")

        self.assertEqual(browser.profile_id, "user2")


if __name__ == "__main__":
    unittest.main()


class FollowupRefusalTest(unittest.TestCase):
    """Google отвечает на уточнение блоком «контент не сгенерирован»."""

    def test_refusal_is_reported_instead_of_waiting_out_the_timeout(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ, {"AIFARM_PROFILE_ROOT": root, "AIFARM_PROFILE_COUNT": "2"}
        ):
            os.environ.pop("AIFARM_PROFILE_PATH", None)
            browser = Browser()
            with patch.object(Browser, "_type_query"), \
                 patch.object(Browser, "_dump_page"), \
                 patch.object(Browser, "_read_pause_seconds", return_value=0.0), \
                 patch.object(Browser, "_session") as session_of:
                session = session_of.return_value
                session.transcript = []
                page = session.page

                def locator(selector):
                    found = page.locator.mock_locators[selector]
                    return found

                page.locator.mock_locators = {}
                page.locator.side_effect = locator
                from unittest.mock import MagicMock
                answers, errors = MagicMock(), MagicMock()
                answers.count.return_value = 1
                answers.last.inner_text.return_value = "прошлый ответ"
                errors.count.return_value = 1
                errors.last.inner_text.return_value = (
                    "Un probl\u00e8me est survenu. Le contenu n'a pas \u00e9t\u00e9 g\u00e9n\u00e9r\u00e9."
                )
                page.locator.mock_locators.update({
                    ANSWER_ERROR: errors,
                })
                page.locator.side_effect = lambda s: errors if s == ANSWER_ERROR else answers

                with self.assertRaises(AnswerRefusedError) as raised:
                    browser.ask_followup("u", "worker:abcd1234", "уточнение")

            self.assertIn("Le contenu", str(raised.exception))
            self.assertEqual(session.transcript, [])  # неотвеченный ход в контекст не пишем
