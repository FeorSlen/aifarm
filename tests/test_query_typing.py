import os
import random
import statistics
import tempfile
import unittest
from unittest.mock import patch

from mcp.server.mcpserver.exceptions import ToolError

from src.api.mcp import (
    FIRST_QUERY_MAX_CHARS,
    FOLLOWUP_QUERY_MAX_CHARS,
    INSTRUCTIONS,
    _check_question_length,
)
from src.worker.browser import (
    FOLLOWUP_READ_MAX_S,
    FOLLOWUP_READ_MIN_S,
    HUMAN_TAIL_CHARS,
    KEY_DELAY_MIN_MS,
    Browser,
    _followup_key_layout,
)


class FakeKeyboard:
    def __init__(self):
        self.typed = []
        self.pressed = []

    def type(self, ch):
        self.typed.append(ch)

    def press(self, key):
        self.pressed.append(key)


class FollowupKeyLayoutTests(unittest.TestCase):
    def test_converts_russian_letters_to_the_same_english_keys(self):
        self.assertEqual(
            _followup_key_layout("Почему листья желтеют?"),
            "Gjxtve kbcnmz ;tknt.n?",
        )

    def test_preserves_existing_latin_numbers_and_punctuation(self):
        self.assertEqual(_followup_key_layout("Python 3.12 — это что?"), "Python 3.12 — 'nj xnj?")


class FakePage:
    def __init__(self):
        self.keyboard = FakeKeyboard()

    def wait_for_timeout(self, _ms):
        pass


class FakeLocator:
    def __init__(self):
        self.filled = None
        self.clicks = 0

    def click(self):
        self.clicks += 1

    def fill(self, text):
        self.filled = text


class QueryTypingTest(unittest.TestCase):
    """Длинный промпт нельзя набирать посимвольно: это XHR в /complete/search
    почти на каждую клавишу и минуты стука — так профили и уезжали на /sorry/."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"AIFARM_PROFILE_PATH": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.browser = Browser()
        self.page = FakePage()
        self.box = FakeLocator()

    def test_long_query_is_pasted_except_the_tail(self):
        query = "Проведи подробное исследование. " * 40  # ~1280 символов

        self.browser._type_query(self.page, self.box, query)

        self.assertEqual(self.box.filled, query[:-HUMAN_TAIL_CHARS])
        self.assertEqual("".join(self.page.keyboard.typed), query[-HUMAN_TAIL_CHARS:])
        self.assertEqual(self.page.keyboard.pressed, ["End"])

    def test_short_query_is_typed_by_hand(self):
        query = "столица Франции"

        self.browser._type_query(self.page, self.box, query)

        self.assertIsNone(self.box.filled)
        self.assertEqual("".join(self.page.keyboard.typed), query)


class TypingRhythmTest(unittest.TestCase):
    """Ровный uniform-интервал — машинный признак сам по себе, независимо от скорости."""

    TEXT = "Проведи подробное, фактологически аккуратное исследование темы. " * 6

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"AIFARM_PROFILE_PATH": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        random.seed(7)
        browser = Browser()
        self.delays = [browser._key_delay(ch) for ch in self.TEXT]

    def test_speed_stays_in_a_human_band(self):
        mean = statistics.fmean(self.delays)

        self.assertGreaterEqual(min(self.delays), KEY_DELAY_MIN_MS)
        self.assertTrue(110 <= mean <= 190, f"{mean:.0f} мс/символ вне живого диапазона")

    def test_pauses_land_on_word_and_sentence_boundaries(self):
        inside = [d for ch, d in zip(self.TEXT, self.delays) if ch.isalpha()]
        boundary = [d for ch, d in zip(self.TEXT, self.delays) if ch in " ,."]

        self.assertGreater(statistics.fmean(boundary), statistics.fmean(inside))
        # правый хвост: у равномерного распределения его нет
        self.assertGreater(statistics.fmean(self.delays), statistics.median(self.delays))


class SearchQueryLimitTest(unittest.TestCase):
    """Первый вопрос идёт в строку google.com, продолжение — в поле AI Mode."""

    def test_first_question_is_capped_and_points_at_the_two_step_flow(self):
        _check_question_length("я" * FIRST_QUERY_MAX_CHARS, None)  # ровно предел — ок

        with self.assertRaises(ToolError) as raised:
            _check_question_length("я" * (FIRST_QUERY_MAX_CHARS + 1), None)

        message = str(raised.exception)
        self.assertIn(str(FIRST_QUERY_MAX_CHARS), message)
        self.assertIn("tab_id", message)

    def test_followup_allows_a_longer_question(self):
        _check_question_length("я" * FOLLOWUP_QUERY_MAX_CHARS, "worker1:abc")

        with self.assertRaises(ToolError):
            _check_question_length("я" * (FOLLOWUP_QUERY_MAX_CHARS + 1), "worker1:abc")

    def test_server_instructions_explain_safe_search_usage(self):
        self.assertIn("на английском", INSTRUCTIONS)
        self.assertIn("1–3 минуты", INSTRUCTIONS)
        self.assertIn("не запускайте", INSTRUCTIONS)
        self.assertIn("tab_id", INSTRUCTIONS)


class ReadScrollTest(unittest.TestCase):
    """Сессия, сгенерировавшая простыню и не сдвинувшая её ни на пиксель, — не читатель."""

    class FakeMouse:
        def __init__(self):
            self.wheel_calls = []

        def wheel(self, dx, dy):
            self.wheel_calls.append((dx, dy))

    class ScrollPage(FakePage):
        def __init__(self):
            super().__init__()
            self.mouse = ReadScrollTest.FakeMouse()
            self.waits = []

        def wait_for_timeout(self, ms):
            self.waits.append(ms)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"AIFARM_PROFILE_PATH": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.browser = Browser()
        self.page = self.ScrollPage()

    def test_gesture_is_many_small_ticks_not_one_jump(self):
        self.browser._scroll_gesture(self.page, 400)

        deltas = [dy for _, dy in self.page.mouse.wheel_calls]
        self.assertGreaterEqual(len(deltas), 6)        # жест, а не телепорт
        self.assertLess(max(deltas), 60)               # ни одного прыжка на пол-экрана
        self.assertAlmostEqual(sum(deltas), 400, delta=80)
        self.assertTrue(all(dx == 0 for dx, _ in self.page.mouse.wheel_calls))

    def test_gesture_accelerates_then_slows_down(self):
        self.browser._scroll_gesture(self.page, 400)

        deltas = [dy for _, dy in self.page.mouse.wheel_calls]
        middle = deltas[len(deltas) // 2]
        self.assertGreater(middle, deltas[0])          # разгон
        self.assertGreater(middle, deltas[-1])         # торможение

    def test_scrolls_down_in_reading_pace(self):
        self.browser._read_scroll(self.page, steps=3)

        deltas = [dy for _, dy in self.page.mouse.wheel_calls]
        self.assertGreater(len(deltas), 18)            # три жеста мелкими щелчками
        self.assertTrue(all(dy > 0 for dy in deltas))  # вниз, а не куда попало
        self.assertGreaterEqual(sum(1 for p in self.page.waits if p > 300), 3)  # паузы на чтение

    def test_flag_off_means_no_scrolling(self):
        with patch("src.worker.browser.FLAG_READ_SCROLL", False):
            self.browser._read_scroll(self.page, steps=3)

        self.assertEqual(self.page.mouse.wheel_calls, [])


if __name__ == "__main__":
    unittest.main()


class FollowupReadPauseTest(unittest.TestCase):
    """Перед уточняющим вопросом ответ надо прочитать, а не отвечать мгновенно."""

    def make_browser(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ, {"AIFARM_PROFILE_ROOT": root, "AIFARM_PROFILE_COUNT": "1"}
        ):
            os.environ.pop("AIFARM_PROFILE_PATH", None)
            return Browser()

    def test_pause_grows_with_answer_length(self):
        browser = self.make_browser()
        short = browser._read_pause_seconds("к" * 200)
        long = browser._read_pause_seconds("к" * 3000)
        self.assertLess(short, long)

    def test_pause_has_floor_and_ceiling(self):
        browser = self.make_browser()
        self.assertGreaterEqual(browser._read_pause_seconds(""), FOLLOWUP_READ_MIN_S)
        self.assertLessEqual(browser._read_pause_seconds("к" * 100000), FOLLOWUP_READ_MAX_S)

    def test_reading_scrolls_instead_of_freezing(self):
        browser = self.make_browser()
        page = ReadScrollTest.ScrollPage()
        with patch.object(Browser, "_read_pause_seconds", return_value=0.05):
            browser._read_answer(page, "ответ")
        self.assertTrue(page.mouse.wheel_calls, "во время чтения страница должна листаться")
