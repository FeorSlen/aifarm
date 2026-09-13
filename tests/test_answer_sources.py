"""Сноски ответа AI Mode: разбор в браузере и проводка источников до MCP.

Разбор проверяется на настоящем движке (chromium из playwright) по фикстуре
разметки, а не на строках: _ANSWER_JS живёт в DOM, и подменять его регуляркой
означало бы тестировать не то, что выполняется. Фикстура — наша модель разметки
AI Mode, поэтому тест доказывает логику разбора, но не совпадение селекторов с
живой страницей Google; это проверяется дампом (AIFARM_DUMP_ANSWER) на проде.
"""

import unittest
from unittest.mock import MagicMock

from src.api.mcp import _sources_block
from src.api.service import Gateway, Job
from src.contract import Result, Source, Task
from src.worker.browser import (
    CAPTCHA_DOM,
    BlockedError,
    Browser,
    _ANSWER_JS,
    _check_blocked,
    _valid_sources,
)
from src.worker.worker import handle

# Разметка одного хода AI Mode: сноска в sup, сноска через классический
# редирект /url?q=, живая сноска AI Mode (пустая ссылка на /goto?url=<токен>
# рядом с кнопкой, название — в aria-label; снято с дампа настоящей страницы),
# обычные ссылки в тексте, служебная ссылка Google и UI-обвязка под денойз.
FIXTURE = """
<html><body>
<div data-container-id="main-col">
  <h1>Snow</h1>
  <p>Snow scatters light.<sup><a href="https://www.example.com/optics"><span>1</span></a></sup>
  Fresh snow reflects sunlight.<sup><a
    href="/url?q=https%3A%2F%2Fscience.example.org%2Falbedo%3Fref%3Dai&amp;sa=U">2</a></sup></p>
  <p>Python 3.14 is current.<span class="chip"><span aria-hidden="true">&nbsp;</span><a
    target="_blank" rel="noopener"
    aria-label="Python.org (+1) – Welcome to Python.org. Related results"
    href="/goto?url=CAESTwHrOzAV_token"></a><button
    aria-label="Python.org (+1) – Welcome to Python.org. Related results"></button></span></p>
  <p>Winner: <span role="button"><a
    href="/goto?url=CAESZAHrOzAV_rider">Tadej Pogačar</a></span>.</p>
  <p>See <a href="https://www.example.com/optics#top">the optics primer</a>,
  the <a href="https://data.example.net/2024">2024</a> dataset
  and <a href="https://support.google.com/websearch">Google help</a>.</p>
  <button>Share</button>
  <div role="button">Feedback<svg></svg></div>
  <span aria-hidden="true">bullet</span>
</div>
</body></html>
"""


class AnswerParsingTest(unittest.TestCase):
    """_ANSWER_JS на живом DOM."""

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:  # pragma: no cover - зависит от окружения
            raise unittest.SkipTest(f"playwright недоступен: {error}") from error
        cls._pw = sync_playwright().start()
        try:
            cls._browser = cls._pw.chromium.launch(args=["--no-sandbox"])
        except Exception as error:  # pragma: no cover - браузер не установлен
            cls._pw.stop()
            raise unittest.SkipTest(f"chromium недоступен: {error}") from error

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()
        cls._pw.stop()

    def parse(self, denoise=True):
        page = self._browser.new_page()
        try:
            # Отдаём фикстуру с адреса google.com: относительный /url?q=... в
            # разборе разрешается относительно location.href.
            page.route("**/*", lambda route: route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=FIXTURE
            ))
            page.goto("https://www.google.com/search?q=snow&udm=50")
            return page.locator("[data-container-id='main-col']").evaluate(
                _ANSWER_JS, {"denoise": denoise}
            )
        finally:
            page.close()

    def test_footnotes_become_numbered_markers_in_text(self):
        text = self.parse()["text"]

        self.assertIn("Snow scatters light. [1]", text)
        self.assertIn("Fresh snow reflects sunlight. [2]", text)

    def test_sources_are_deduplicated_and_google_redirect_is_unwrapped(self):
        sources = self.parse()["sources"]

        self.assertEqual(
            sources,
            [
                # Заголовок взят у обычной ссылки на тот же адрес: у чипа-сноски
                # текст — сам номер, названием он быть не может.
                {"index": 1, "url": "https://www.example.com/optics",
                 "title": "the optics primer"},
                {"index": 2, "url": "https://science.example.org/albedo?ref=ai",
                 "title": "science.example.org"},
                {"index": 3, "url": "https://www.google.com/goto?url=CAESTwHrOzAV_token",
                 "title": "Python.org (+1) – Welcome to Python.org. Related results"},
                {"index": 4, "url": "https://www.google.com/goto?url=CAESZAHrOzAV_rider",
                 "title": "Tadej Pogačar"},
                {"index": 5, "url": "https://data.example.net/2024",
                 "title": "data.example.net"},
            ],
        )

    def test_ai_mode_footnote_keeps_its_place_in_the_sentence(self):
        text = self.parse()["text"]

        self.assertIn("Python 3.14 is current. [3]", text)

    def test_linked_entity_inside_a_chip_keeps_its_text(self):
        # В AI Mode ссылками обёрнуты и сущности внутри предложения; удаление
        # чипа целиком вырезало из ответа имена (наблюдалось на живой выдаче).
        text = self.parse()["text"]

        self.assertIn("Winner: Tadej Pogačar [4]", text)

    def test_inline_link_keeps_its_text_and_gets_the_same_number(self):
        text = self.parse()["text"]

        self.assertIn("the optics primer [1]", text)

    def test_google_own_links_are_not_sources(self):
        parsed = self.parse()

        self.assertNotIn("support.google.com", str(parsed["sources"]))
        self.assertIn("Google help", parsed["text"])  # текст ссылки остаётся
        self.assertNotIn("[6]", parsed["text"])

    def test_numeric_link_text_is_not_mistaken_for_a_footnote(self):
        text = self.parse()["text"]

        self.assertIn("the 2024 [5] dataset", text)

    def test_denoise_removes_ui_but_keeps_markers(self):
        text = self.parse()["text"]

        for noise in ("Share", "Feedback", "bullet"):
            self.assertNotIn(noise, text)
        self.assertIn("[1]", text)

    def test_markers_survive_with_denoise_off(self):
        parsed = self.parse(denoise=False)

        self.assertIn("[1]", parsed["text"])
        self.assertIn("Share", parsed["text"])  # стрип выключен
        self.assertEqual(len(parsed["sources"]), 5)


class SourceValidationTest(unittest.TestCase):
    def test_only_well_formed_http_sources_pass(self):
        self.assertEqual(
            _valid_sources([
                {"index": 1, "url": "https://a.example/x", "title": " A "},
                {"index": 2, "url": "javascript:alert(1)", "title": "bad"},
                {"index": 0, "url": "https://b.example/", "title": "zero"},
                {"index": "3", "url": "https://c.example/", "title": "str index"},
                {"index": 4, "url": "https://d.example/", "title": None},
                "мусор",
            ]),
            [
                {"index": 1, "url": "https://a.example/x", "title": "A"},
                {"index": 4, "url": "https://d.example/", "title": ""},
            ],
        )

    def test_missing_payload_is_empty_list(self):
        self.assertEqual(_valid_sources(None), [])

    def test_parse_failure_falls_back_to_plain_text(self):
        browser = Browser.__new__(Browser)
        answer = MagicMock()
        answer.evaluate.side_effect = RuntimeError("страница уехала")

        text, sources = browser._answer_parts(answer, "  сырой ответ  ")

        self.assertEqual(text, "сырой ответ")
        self.assertEqual(sources, [])


class BlockDetectionTest(unittest.TestCase):
    def test_captcha_without_sorry_redirect_is_a_block(self):
        page = MagicMock()
        page.url = "https://www.google.com/search?q=snow"
        page.locator.return_value.count.return_value = 1

        with self.assertRaises(BlockedError):
            _check_blocked(page, deep=True)
        page.locator.assert_called_once_with(CAPTCHA_DOM)

    def test_shallow_check_does_not_touch_the_page(self):
        page = MagicMock()
        page.url = "https://www.google.com/search?q=snow"

        _check_blocked(page)

        page.locator.assert_not_called()


class SourcesPlumbingTest(unittest.TestCase):
    def test_worker_returns_sources_of_the_last_answer(self):
        class BrowserStub:
            def ask_followup(self, *_args):
                pass

            def get_last_ai_answer(self, *_args):
                return "ответ [1]"

            def last_sources(self, *_args):
                return [{"index": 1, "url": "https://a.example/x", "title": "A"}]

        result = handle(
            BrowserStub(),
            Task(user_session_id="user", session_id="worker:tab", query="вопрос"),
        )

        self.assertEqual(result.sources, [Source(index=1, url="https://a.example/x", title="A")])

    def test_gateway_publishes_sources_with_the_done_job(self):
        gateway = Gateway()
        gateway.jobs["job"] = Job(user_session_id="user", tab_id=None)

        gateway._record_result("job", Result(
            session_id="worker:tab",
            answer="ответ [1]",
            sources=[Source(index=1, url="https://a.example/x", title="A")],
        ))

        job = gateway.get_job("user", "job")
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["sources"], [{"index": 1, "url": "https://a.example/x", "title": "A"}])

    def test_answer_without_sources_stays_empty(self):
        gateway = Gateway()
        gateway.jobs["job"] = Job(user_session_id="user", tab_id=None)

        gateway._record_result("job", Result(session_id="worker:tab", answer="ответ"))

        self.assertEqual(gateway.get_job("user", "job")["sources"], [])

    def test_mcp_lists_sources_under_their_marker_numbers(self):
        block = _sources_block([
            {"index": 1, "url": "https://a.example/x", "title": "A"},
            {"index": 2, "url": "https://b.example/y", "title": ""},
        ])

        self.assertEqual(
            block,
            "Источники:\n[1] A — https://a.example/x\n[2] https://b.example/y",
        )

    def test_mcp_skips_the_block_without_sources(self):
        self.assertIsNone(_sources_block([]))


if __name__ == "__main__":
    unittest.main()
