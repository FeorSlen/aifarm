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
from src.worker.browser import AI_ANSWER, ANSWER_ERROR, FOLLOWUP_INPUT, Browser, _is_image_accept

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

    def test_attachments_are_not_allowed_into_a_new_conversation(self):
        with self.assertRaises(ValidationError):
            Task(user_session_id="u", query="what is this", attachments=[IMAGE])

    def test_message_needs_text_or_attachments(self):
        with self.assertRaises(ValidationError):
            Task(user_session_id="u", session_id="w:t", query="   ")

    def test_control_tasks_carry_no_attachments(self):
        Task(user_session_id="u")                     # список вкладок
        Task(user_session_id="u", session_id="w:t")   # закрыть вкладку
        with self.assertRaises(ValidationError):
            Task(user_session_id="u", session_id="w:t", attachments=[IMAGE])


class McpMessageCheckTest(unittest.TestCase):
    def test_attachments_without_tab_are_rejected(self):
        with self.assertRaises(ToolError):
            _check_message("what is this", None, [FILE_ID])

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
