#!/usr/bin/env python3
"""Проверка разбора сносок AI Mode на живой выдаче.

Тесты доказывают логику разбора на фикстуре, но не совпадение с текущей
разметкой Google — она и есть главный риск этой фичи. Скрипт задаёт один
вопрос и печатает зонд DOM, источники и ответ, чтобы дрейф разметки был виден
сразу: ноль источников при непустом anchors — селекторы поехали.

Локально (свой X уже занят курсором — humanize двигает настоящую мышь):

    Xvfb :98 -screen 0 1920x1080x24 -nolisten tcp &
    DISPLAY=:98 AIFARM_PROFILE_ROOT=<копия пула> AIFARM_PROFILE_COUNT=1 \
        python scripts/check_sources.py "who won the 2026 tour de france" "and who was second"

Второй аргумент необязателен: с ним скрипт задаёт уточняющий вопрос в той же
вкладке и печатает разбор обоих ходов.

Внутри контейнера воркера — так же, но с DISPLAY=:99 (см. check_followup.py).
Профиль берётся из пула как в бою, поэтому запускать на КОПИИ пула: вопрос
тратит норму профиля, а /sorry/ поставит маркер.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.worker.browser import AI_ANSWER, Browser  # noqa: E402

QUESTION = sys.argv[1] if len(sys.argv) > 1 else "what is the latest python version"
FOLLOWUP = sys.argv[2] if len(sys.argv) > 2 else None

# Что смотреть глазами: сколько ссылок в ответе вообще есть и в каком они виде.
# Сноска AI Mode — пустая ссылка на /goto?url=<токен> рядом с кнопкой; название
# источника лежит в aria-label, настоящего адреса в разметке нет нигде.
PROBE_JS = """(el) => ({
    anchors: el.querySelectorAll('a[href]').length,
    goto: el.querySelectorAll('a[href^="/goto?url="]').length,
    sups: el.querySelectorAll('sup').length,
    buttons: el.querySelectorAll('button, [role="button"]').length,
    hrefs: Array.from(el.querySelectorAll('a[href]')).slice(0, 10)
        .map((a) => a.getAttribute('href').slice(0, 60)),
})"""


def report(browser, session_id, title):
    text = browser.get_last_ai_answer("check", session_id)
    sources = browser.last_sources("check", session_id)
    page = browser._session("check", session_id).page
    probe = page.locator(AI_ANSWER).last.evaluate(PROBE_JS)

    print(f"\n=== {title}: зонд DOM ===")
    for key, value in probe.items():
        print(f"{key}: {value}")
    print(f"\n=== {title}: источники ({len(sources)}) ===")
    for source in sources:
        print(f"[{source['index']}] {source['title']} — {source['url']}")
    # Сравниваем именно со сносками: четыре служебные ссылки Google (справка,
    # приватность, условия) есть в любом ответе, даже когда Google не сослался
    # ни на что — это норма, а не дрейф разметки.
    if probe["goto"] and not sources:
        print("СНОСКИ ЕСТЬ, ИСТОЧНИКОВ НЕТ — разметка поехала, смотреть _ANSWER_JS")
    print(f"\n=== {title}: ответ ===")
    print(text)


def main():
    browser = Browser(worker_id="check")
    try:
        print(f"профиль: {browser.profile_id}", flush=True)
        started = time.monotonic()
        session_id = browser.new_search_session("check", QUESTION)
        report(browser, session_id, "вопрос")
        print(f"\n⏱ вопрос: {time.monotonic() - started:.1f} с (от старта до готового ответа)", flush=True)
        if FOLLOWUP:
            started = time.monotonic()
            browser.ask_followup("check", session_id, FOLLOWUP)
            sent = time.monotonic()
            report(browser, session_id, "уточнение")
            print(f"\n⏱ уточнение: {time.monotonic() - started:.1f} с всего; "
                  f"до появления ответа {sent - started:.1f} с (чтение + ввод + ожидание Google), "
                  f"дописывание и чтение ответа {time.monotonic() - sent:.1f} с", flush=True)
    finally:
        browser.shutdown()


if __name__ == "__main__":
    main()
