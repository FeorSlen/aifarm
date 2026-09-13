import base64
import unittest
from unittest.mock import MagicMock, call

from mcp.types import ImageContent
from playwright._impl._errors import TargetClosedError

from src.api.mcp import _diagnostic_error
from src.api.service import Gateway, Job
from src.contract import Result, Task
from src.worker.browser import (
    AI_ANSWER,
    AI_MODE_ARMED,
    AI_MODE_CHIP,
    AI_MODE_LINK,
    FOLLOWUP_INPUT,
    GOOGLE_URL,
    SEARCH_BOX,
    AnswerRefusedError,
    AnswerUnavailableError,
    Browser,
    _ai_mode_url,
)
from src.worker.worker import handle


class ScreenshotPage:
    def screenshot(self, **kwargs):
        self.kwargs = kwargs
        return b"png-bytes"


class DiagnosticScreenshotTest(unittest.TestCase):
    def test_ai_mode_url_preserves_search_and_replaces_existing_mode(self):
        self.assertEqual(
            _ai_mode_url("https://www.google.com/search?q=snow&client=test&udm=2"),
            "https://www.google.com/search?q=snow&client=test&udm=50",
        )

    def test_missing_ai_mode_button_falls_back_to_direct_url(self):
        browser = Browser.__new__(Browser)
        browser._accept_cookies = lambda _page: None
        browser._type_query = lambda *_args: None

        page = MagicMock()
        page.url = "https://www.google.com/search?q=snow&client=test"
        box = MagicMock()
        link = MagicMock()
        link.wait_for.side_effect = TimeoutError("button missing")
        answer = MagicMock()
        link_locator = MagicMock(first=link)
        answer_locator = MagicMock(last=answer)
        chip = MagicMock()
        chip.click.side_effect = TimeoutError("chip missing")
        page.locator.side_effect = lambda selector: {
            SEARCH_BOX: box,
            AI_MODE_CHIP: MagicMock(first=chip),
            AI_MODE_ARMED: MagicMock(),
            AI_MODE_LINK: link_locator,
            AI_ANSWER: answer_locator,
        }[selector]

        browser._open_ai_mode(page, "why is snow white")

        self.assertEqual(
            page.goto.call_args_list,
            [
                call(GOOGLE_URL, wait_until="domcontentloaded", timeout=45000),
                call(
                    "https://www.google.com/search?q=snow&client=test&udm=50",
                    wait_until="domcontentloaded",
                    timeout=45000,
                ),
            ],
        )
        link.click.assert_not_called()
        answer.wait_for.assert_called_once()

    def _chip_page(self, url_after_enter):
        """Главная, где плашка AI Mode срабатывает; Enter ведёт на url_after_enter."""
        browser = Browser.__new__(Browser)
        browser._accept_cookies = lambda _page: None
        typed = []
        browser._type_query = lambda _page, _box, text: typed.append(text)

        page = MagicMock()
        page.url = "https://www.google.com/"
        box = MagicMock()
        box.press.side_effect = lambda _key: setattr(page, "url", url_after_enter)
        chip = MagicMock()
        link = MagicMock()
        answer = MagicMock()
        page.locator.side_effect = lambda selector: {
            SEARCH_BOX: box,
            AI_MODE_CHIP: MagicMock(first=chip),
            AI_MODE_ARMED: MagicMock(),
            AI_MODE_LINK: MagicMock(first=link),
            AI_ANSWER: MagicMock(last=answer),
        }[selector]
        return browser, page, chip, link, answer, typed

    def test_ai_mode_chip_goes_straight_to_ai_mode(self):
        browser, page, chip, link, answer, typed = self._chip_page(
            "https://www.google.com/search?q=snow&source=hp&udm=50"
        )

        browser._open_ai_mode(page, "why is snow white")

        chip.click.assert_called_once()
        self.assertEqual(page.goto.call_count, 1)  # только главная, без выдачи
        link.wait_for.assert_not_called()
        self.assertEqual(typed, ["why is snow white"])
        answer.wait_for.assert_called_once()

    def test_chip_that_landed_on_plain_results_continues_via_tab_without_retyping(self):
        browser, page, chip, link, answer, typed = self._chip_page(
            "https://www.google.com/search?q=snow&source=hp"
        )

        browser._open_ai_mode(page, "why is snow white")

        link.click.assert_called_once()
        self.assertEqual(typed, ["why is snow white"])  # вопрос отправлен один раз
        self.assertEqual(page.goto.call_count, 1)
        answer.wait_for.assert_called_once()

    def test_current_followup_input_matches_live_ai_mode_markup(self):
        self.assertEqual(FOLLOWUP_INPUT.split(",", 1)[0], "textarea.ITIRGe:visible")

    def test_browser_captures_viewport_png_as_base64(self):
        page = ScreenshotPage()

        screenshot = Browser._capture_screenshot(page)

        self.assertEqual(screenshot, base64.b64encode(b"png-bytes").decode("ascii"))
        self.assertEqual(
            page.kwargs, {"type": "png", "full_page": False, "timeout": 5000}
        )

    def test_ai_mode_open_failure_is_returned_with_screenshot(self):
        class Page(ScreenshotPage):
            url = "https://www.google.com/search?q=test"
            def bring_to_front(self):
                pass

        page = Page()
        browser = Browser.__new__(Browser)
        browser._profile_budget = 1
        browser._expire_sessions = lambda: None
        browser._rotate_by_load = lambda: None
        browser._ensure_browser = lambda: object()
        browser._take_free_page = lambda _context: page
        browser._dump_page = lambda *_args: None

        def fail_open(_page, _query):
            raise RuntimeError("click timeout")

        browser._open_ai_mode = fail_open

        with self.assertRaises(AnswerUnavailableError) as raised:
            browser.new_search_session("user", "question")

        self.assertEqual(raised.exception.screenshot, base64.b64encode(b"png-bytes").decode("ascii"))

    def test_dead_browser_is_restarted_before_new_search(self):
        browser = Browser.__new__(Browser)
        dead_context = object()
        live_context = object()
        page = object()
        browser._context = dead_context
        browser._cm = MagicMock()
        browser._sessions = {"old": object()}
        browser._profile_dir = "/tmp/profile"
        browser._ensure_browser = MagicMock(side_effect=[dead_context, live_context])
        browser._take_free_page = MagicMock(
            side_effect=[TargetClosedError("browser closed"), page]
        )

        self.assertIs(browser._take_search_page(), page)
        self.assertEqual(
            browser._take_free_page.call_args_list,
            [call(dead_context), call(live_context)],
        )
        self.assertEqual(browser._sessions, {})

    def test_worker_preserves_error_code_and_screenshot(self):
        class BrowserStub:
            def ask_followup(self, *_args):
                raise AnswerUnavailableError("нет ответа", "cG5n")

        result = handle(
            BrowserStub(),
            Task(user_session_id="user", session_id="worker:tab", query="question"),
        )

        self.assertEqual(result.error, "answer_unavailable")
        self.assertEqual(result.screenshot, "cG5n")

    def test_gateway_returns_screenshot_only_with_failed_job(self):
        gateway = Gateway()
        gateway.jobs["job"] = Job(user_session_id="user", tab_id="worker:tab")

        gateway._record_result(
            "job", Result(error="answer_refused", screenshot="cG5n")
        )

        job = gateway.get_job("user", "job")
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["screenshot"], "cG5n")

    def test_mcp_error_contains_png_image(self):
        result = _diagnostic_error("нет ответа", "cG5n")

        self.assertTrue(result.is_error)
        self.assertEqual(result.content[0].text, "нет ответа")
        self.assertIsInstance(result.content[1], ImageContent)
        self.assertEqual(result.content[1].data, "cG5n")
        self.assertEqual(result.content[1].mime_type, "image/png")

    def test_refusal_keeps_diagnostic_payload(self):
        error = AnswerRefusedError("отказ", "cG5n")
        self.assertEqual(error.code, "answer_refused")
        self.assertEqual(error.screenshot, "cG5n")


if __name__ == "__main__":
    unittest.main()
