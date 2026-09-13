"""Сообщение с вложениями: контракт, хранилище загрузок, порядок действий в браузере.

Разметку AI Mode (кнопка «+», input по accept, карточка с прогрессом) тесты не
моделируют — она проверена живыми прогонами; здесь то, что можно проверить без
Google: кто и что может отправить, где лежит файл и в каком порядке идут шаги.
"""

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from src.api import uploads
from src.api.mcp import _check_message
from src.api.service import Gateway, Job
from src.contract import Attachment, Result, Task
from src.worker.browser import (
    AI_ANSWER,
    AI_MODE_LINK,
    ANSWER_ERROR,
    FOLLOWUP_INPUT,
    SEARCH_BOX,
    AttachmentError,
    Browser,
    _is_image_accept,
)
from src.worker.worker import handle

IMAGE = Attachment(path="/tmp/x/cat.png", name="cat.png", kind="image")
FILE_ID = "a" * 32


class Reader:
    """Как UploadFile: async read(n), отдаёт поток мелкими кусками."""

    def __init__(self, data: bytes, chunk: int = 4):
        self.data, self.chunk = data, chunk

    async def read(self, n):
        size = min(n, self.chunk)
        piece, self.data = self.data[:size], self.data[size:]
        return piece


class MessageContractTest(unittest.TestCase):
    def test_attachments_only_message_in_session_is_valid(self):
        task = Task(user_session_id="u", session_id="w:t", query="", attachments=[IMAGE])
        self.assertEqual(task.attachments, [IMAGE])

    def test_text_and_attachments_together_are_valid(self):
        Task(user_session_id="u", session_id="w:t", query="what is this", attachments=[IMAGE])

    def test_new_conversation_may_carry_attachments_with_its_question(self):
        Task(user_session_id="u", query="what is this", attachments=[IMAGE])

    def test_new_conversation_starts_with_text(self):
        with self.assertRaises(ValidationError):
            Task(user_session_id="u", query="", attachments=[IMAGE])

    def test_message_needs_text_or_attachments(self):
        with self.assertRaises(ValidationError):
            Task(user_session_id="u", session_id="w:t", query="   ")

    def test_control_tasks_carry_no_attachments(self):
        Task(user_session_id="u")                     # список вкладок
        Task(user_session_id="u", session_id="w:t")   # закрыть вкладку
        with self.assertRaises(ValidationError):
            Task(user_session_id="u", session_id="w:t", attachments=[IMAGE])


class McpMessageCheckTest(unittest.TestCase):
    def test_new_conversation_takes_attachments_with_a_question(self):
        _check_message("what is this", None, [FILE_ID])
        with self.assertRaises(ToolError):
            _check_message("", None, [FILE_ID])

    def test_empty_question_is_allowed_only_with_attachments(self):
        with self.assertRaises(ToolError):
            _check_message("", "w:t", [])
        _check_message("", "w:t", [FILE_ID])


class UploadStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(uploads, "UPLOAD_DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def save(self, user, name, data):
        return asyncio.run(uploads.save(user, name, Reader(data)))

    def test_owner_gets_the_file_back_under_its_original_name(self):
        meta = self.save("user-a", "../../etc/Recipe.TXT", b"347 grams")

        self.assertEqual((meta["name"], meta["kind"], meta["size"]), ("Recipe.TXT", "file", 9))
        [attachment] = uploads.resolve("user-a", [meta["file_id"]])
        self.assertTrue(Path(attachment.path).is_absolute())
        self.assertEqual(Path(attachment.path).read_bytes(), b"347 grams")
        self.assertEqual(attachment.name, "Recipe.TXT")

    def test_image_kind_follows_ai_mode_accept_list(self):
        self.assertEqual(self.save("u", "photo.HEIC", b"x")["kind"], "image")
        self.assertEqual(self.save("u", "scan.pdf", b"x")["kind"], "file")

    def test_other_tenant_cannot_send_the_file(self):
        meta = self.save("user-a", "a.png", b"x")
        with self.assertRaises(uploads.UploadError):
            uploads.resolve("user-b", [meta["file_id"]])

    def test_oversized_upload_is_rejected_and_not_kept(self):
        with patch.object(uploads, "UPLOAD_MAX_BYTES", 5), \
                self.assertRaises(uploads.UploadTooLarge):
            self.save("u", "big.bin", b"123456789")
        self.assertEqual(list((Path(self.tmp.name) / "u").iterdir()), [])

    def test_empty_upload_is_rejected(self):
        with self.assertRaises(uploads.UploadError):
            self.save("u", "empty.txt", b"")

    def test_expired_upload_cannot_be_sent(self):
        meta = self.save("u", "a.png", b"x")
        old = time.time() - uploads.UPLOAD_TTL_SECONDS - 10
        os.utime(Path(self.tmp.name) / "u" / meta["file_id"], (old, old))
        with self.assertRaises(uploads.UploadError):
            uploads.resolve("u", [meta["file_id"]])

    def test_file_id_cannot_walk_the_filesystem(self):
        with self.assertRaises(uploads.UploadError):
            uploads.resolve("u", ["../../etc/passwd"])

    def test_too_many_attachments_are_rejected(self):
        ids = [f"{n:032x}" for n in range(uploads.MAX_ATTACHMENTS + 1)]
        with self.assertRaises(uploads.UploadError):
            uploads.resolve("u", ids)


class AttachInFollowupTest(unittest.TestCase):
    def test_image_input_is_recognized_by_accept(self):
        self.assertTrue(_is_image_accept("image/avif,image/bmp,image/jpeg,image/png"))
        self.assertFalse(_is_image_accept(""))       # input файлов у поля уточнения
        self.assertFalse(_is_image_accept("*/*"))    # input файлов на главной

    def followup(self, query, attachments):
        """ask_followup на фейковой странице; возвращает порядок шагов и сессию."""
        browser = Browser.__new__(Browser)
        browser._profile_budget = 5
        steps = []
        page = MagicMock()
        answers, errors, box = MagicMock(), MagicMock(), MagicMock()
        answers.count.return_value = 1
        answers.last.inner_text.return_value = "прошлый ответ"
        errors.count.return_value = 0
        page.locator.side_effect = lambda selector: {
            AI_ANSWER: answers, ANSWER_ERROR: errors, FOLLOWUP_INPUT: MagicMock(first=box),
        }[selector]
        box.press.side_effect = lambda key: steps.append(("press", key))
        session = MagicMock(page=page, transcript=[], answer_read=True)
        with patch.object(Browser, "_session", return_value=session), \
             patch.object(Browser, "_attach",
                          side_effect=lambda _page, items: steps.append(
                              ("attach", [item.name for item in items]))), \
             patch.object(Browser, "_type_query",
                          side_effect=lambda _page, _box, text: steps.append(("type", text))):
            browser.ask_followup("u", "w:t", query, attachments)
        return steps, session

    def test_attachments_are_attached_before_the_text_is_typed(self):
        steps, _ = self.followup("what is this", [IMAGE])

        self.assertEqual(steps, [("attach", ["cat.png"]), ("type", "what is this"), ("press", "Enter")])

    def test_attachments_only_message_is_sent_with_empty_text(self):
        steps, session = self.followup("", [IMAGE])

        self.assertEqual(steps, [("attach", ["cat.png"]), ("type", ""), ("press", "Enter")])
        self.assertEqual(session.transcript, [["", None]])

    def test_plain_followup_does_not_open_the_attach_menu(self):
        steps, _ = self.followup("and why", [])

        self.assertEqual(steps, [("type", "and why"), ("press", "Enter")])


class FirstQuestionAttachmentsTest(unittest.TestCase):
    """Первый вопрос с вложениями: быстрый путь через плашку и запасной."""

    def open_ai_mode(self, armed, attach_error=None):
        browser = Browser.__new__(Browser)
        browser._accept_cookies = lambda _page: None
        steps = []
        browser._arm_ai_mode = lambda _page: steps.append("arm") or armed

        def attach(_page, items):
            steps.append(("attach", [item.name for item in items]))
            if attach_error:
                raise attach_error

        browser._attach = attach
        browser._type_query = lambda _page, _box, text: steps.append(("type", text))
        page = MagicMock()
        page.url = "https://www.google.com/"
        box = MagicMock()
        box.press.side_effect = lambda _key: setattr(
            page, "url", "https://www.google.com/search?q=x&udm=50"
        )
        page.locator.side_effect = lambda selector: {
            SEARCH_BOX: box, AI_ANSWER: MagicMock(), AI_MODE_LINK: MagicMock(),
        }[selector]
        pending = browser._open_ai_mode(page, "what is this", attachments=[IMAGE])
        return steps, pending, page

    def test_chip_path_sends_attachments_together_with_the_question(self):
        steps, pending, _ = self.open_ai_mode(armed=True)

        self.assertEqual(steps, ["arm", ("attach", ["cat.png"]), ("type", "what is this")])
        self.assertEqual(pending, [])

    def test_without_chip_attachments_wait_for_the_first_answer(self):
        steps, pending, _ = self.open_ai_mode(armed=False)

        self.assertEqual(steps, ["arm", ("type", "what is this")])
        self.assertEqual(pending, [IMAGE])

    def test_rejected_attachments_restart_from_a_clean_homepage(self):
        steps, pending, page = self.open_ai_mode(armed=True, attach_error=AttachmentError("нет «+»"))

        self.assertEqual(steps, ["arm", ("attach", ["cat.png"]), "arm", ("type", "what is this")])
        self.assertEqual(pending, [IMAGE])
        self.assertEqual(page.goto.call_count, 2)  # ни один файл не уйдёт дважды

    def handle_new(self, pending):
        calls = []

        class BrowserStub:
            def new_search_session(self, _user, query, attachments):
                calls.append(("new", query, [item.name for item in attachments]))
                return "w:t"

            def take_pending_attachments(self, _user, _session):
                return pending

            def get_last_ai_answer(self, _user, _session):
                calls.append(("answer",))
                return "ответ"

            def ask_followup(self, _user, _session, query, attachments=()):
                calls.append(("followup", query, [item.name for item in attachments]))

            def last_sources(self, _user, _session):
                return []

        result = handle(BrowserStub(), Task(user_session_id="u", query="what is this", attachments=[IMAGE]))
        return calls, result

    def test_worker_needs_no_second_message_when_attachments_went_with_the_question(self):
        calls, result = self.handle_new(pending=[])

        self.assertEqual(calls, [("new", "what is this", ["cat.png"]), ("answer",)])
        self.assertEqual(result.session_id, "w:t")

    def test_worker_fallback_sends_attachments_after_the_first_answer(self):
        calls, result = self.handle_new(pending=[IMAGE])

        self.assertEqual(calls, [
            ("new", "what is this", ["cat.png"]),
            ("answer",),                          # дождались ответа на вопрос
            ("followup", "", ["cat.png"]),        # вложения следующим сообщением
            ("answer",),                          # клиенту — ответ на вложения
        ])
        self.assertEqual(result.session_id, "w:t")


class AttachmentFailureMappingTest(unittest.TestCase):
    def test_attachment_failure_keeps_its_code_and_screenshot(self):
        gateway = Gateway()
        gateway.jobs["job"] = Job(user_session_id="u", tab_id="w:t")

        gateway._record_result("job", Result(error="attachment_failed", screenshot="cG5n"))

        job = gateway.get_job("u", "job")
        self.assertEqual(job["error"]["code"], "attachment_failed")
        self.assertEqual(job["screenshot"], "cG5n")


if __name__ == "__main__":
    unittest.main()


class AttachedReadinessTest(unittest.TestCase):
    """ATTACHED_JS на настоящем движке: фикстуры повторяют снятую разметку обоих полей."""

    HOMEPAGE = """
    <form action="/search"><textarea name="q"></textarea>
      <div role="list">
        <div class="f1YNu" role="group" aria-label="cat.png">
          <button aria-label="cat.png" type="button"><img alt="">
            <div log="SearchboxMultimodalLoadingIcon; track:ved"></div></button></div>
        <div class="f1YNu" role="group" aria-label="recipe.txt">
          <button aria-label="recipe.txt" type="button">
            <div log="SearchboxMultimodalLoadingIcon; track:ved"></div></button></div>
      </div>
      <button aria-haspopup="menu" type="button">+</button>
      <button type="submit" {submit}>AI Mode</button>
    </form>"""

    CHAT = """
    <div><textarea class="ITIRGe"></textarea>
      <div role="button" title="cat.png" aria-label="cat.png">
        <div role="progressbar" style="{bar}">…</div></div>
      <button data-xid="input-plate-send-button">send</button>
    </div>"""

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:  # pragma: no cover - зависит от окружения
            raise unittest.SkipTest(f"playwright недоступен: {error}") from error
        from src.worker.browser import ATTACHED_JS
        cls.js = ATTACHED_JS
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

    def ready(self, html, names):
        page = self._browser.new_page()
        try:
            page.set_content(html)
            return page.evaluate(self.js, names)
        finally:
            page.close()

    def test_homepage_waits_while_the_submit_button_is_blocked(self):
        self.assertFalse(self.ready(self.HOMEPAGE.replace("{submit}", 'disabled aria-disabled="true"'),
                                    ["cat.png", "recipe.txt"]))

    def test_homepage_is_ready_when_the_submit_button_unblocks(self):
        self.assertTrue(self.ready(self.HOMEPAGE.replace("{submit}", ""), ["cat.png", "recipe.txt"]))

    def test_missing_card_is_not_ready(self):
        self.assertFalse(self.ready(self.HOMEPAGE.replace("{submit}", ""), ["cat.png", "other.pdf"]))

    def test_chat_waits_for_the_progressbar(self):
        self.assertFalse(self.ready(self.CHAT.replace("{bar}", ""), ["cat.png"]))

    def test_chat_is_ready_when_the_progressbar_hides(self):
        self.assertTrue(self.ready(self.CHAT.replace("{bar}", "display:none"), ["cat.png"]))
