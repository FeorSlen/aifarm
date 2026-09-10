#!/usr/bin/env python3
"""Прогрев одного профиля живым потоком поисков — и заодно замер адреса.

Скрипт держится за ОДИН профиль и гоняет по нему разнотемные поиски, каждый с
продолжением диалога. Профиль не помечается и не ротируется: /sorry/ здесь не
авария, а ожидаемое событие — человек решает капчу в VNC-окне, скрипт через
паузу идёт дальше тем же профилем.

Темы намеренно разбросаны по разным областям: десять запросов подряд об одном
и том же — сами по себе заявка на проверку.

Что меряем: доля первых вопросов, доживших до ответа, и доля продолжений
диалога. Второе и есть проверка адреса — на датацентровом IP оно пока ни разу
не сработало.

Запуск внутри контейнера воркера (воркер перед этим остановить — он держит
профили):

    docker compose run --rm --service-ports -e DISPLAY=:99 worker \
        sh -c 'Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp & sleep 1; \
               x11vnc -display :99 -forever -shared -nopw -rfbport 5900 -listen 0.0.0.0 -quiet & \
               python scripts/warm_traffic.py user1'
"""

import argparse
import os
import random
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Пауза между поисками. Прогрев — это не гонка: ровный поток без пауз и есть
# то, чем мы палимся, а профилю нужна история, а не скорость.
GAP_S = float(os.getenv("AIFARM_WARM_GAP_S", "120"))
# Сколько ждать после /sorry/, прежде чем пробовать снова: время человеку
# заметить капчу в окне и решить её.
CAPTCHA_PAUSE_S = float(os.getenv("AIFARM_WARM_CAPTCHA_PAUSE_S", "90"))
# Потолок на раунд целиком. Внутренние таймауты покрывают конкретные вызовы,
# но зависнуть можно и между ними — тогда браузер сносится и поднимается заново.
ROUND_DEADLINE_S = int(os.getenv("AIFARM_WARM_ROUND_DEADLINE_S", "240"))


class RoundTimeout(Exception):
    """Раунд не уложился в ROUND_DEADLINE_S."""

# Пары «вопрос — продолжение» из разных областей: кухня, право, авто, сад,
# деньги, здоровье, путешествия, животные, ремонт, искусство.
TOPICS = [
    ("как правильно замариновать курицу для духовки", "а сколько её потом запекать"),
    ("что делать если сосед шумит после 23 часов", "а куда жаловаться если участковый молчит"),
    ("почему желтеют листья у монстеры", "а как часто её вообще поливать"),
    ("чем отличается ОСАГО от КАСКО", "а что выгоднее при стаже вождения два года"),
    ("как выбрать беговые кроссовки для асфальта", "а через сколько километров их менять"),
    ("почему в квартире холодные батареи при работающем отоплении", "а как сделать перерасчёт"),
    ("что такое индексный фонд простыми словами", "а какие у него есть риски"),
    ("как отличить настоящий мёд от подделки", "а правда что настоящий засахаривается"),
    ("сколько лететь из Москвы в Ханой", "а нужна ли туда виза"),
    ("почему кошка топчет лапами хозяина", "а почему она при этом мурлычет"),
    ("как убрать царапины с ламината", "а если царапина глубокая до основы"),
    ("что такое ключевая ставка и на что она влияет", "а как она связана с ипотекой"),
    ("какие витамины реально нужны зимой", "а можно ли их набрать из еды"),
    ("как научиться правильно приседать со штангой", "а какой вес брать новичку"),
    ("почему ноутбук шумит и сильно греется", "а как часто менять термопасту"),
    ("чем известен художник Архип Куинджи", "а где сейчас хранятся его картины"),
    ("как выбрать матрас при болях в спине", "а какая жёсткость нужна"),
    ("что приготовить из кабачков кроме оладий", "а как их лучше хранить зимой"),
    ("как оспорить штраф с дорожной камеры", "а сколько дней на это есть"),
    ("почему трескается штукатурка на стене", "а чем её заделать самому"),
    ("что посмотреть в Стамбуле за три дня", "а стоит ли брать музейную карту"),
    ("как понять что рассада вытянулась", "а можно ли её после этого спасти"),
    ("чем отличается эспрессо от американо", "а в каком из них больше кофеина"),
    ("как правильно тормозить на льду на механике", "а если машину уже понесло"),
    ("почему собака грызёт вещи когда остаётся одна", "а как её от этого отучить"),
    ("что такое налоговый вычет за лечение", "а какие документы для него нужны"),
    ("чем костяной фарфор отличается от обычного", "а можно ли его мыть в посудомойке"),
    ("почему машина не заводится в мороз", "а как правильно прикурить от другой"),
    ("как приучить ребёнка читать книги", "а с какого возраста лучше начинать"),
    ("чем полезна скандинавская ходьба", "а как правильно подобрать палки"),
]


def log_line(path: Path, *fields) -> None:
    """Лог переживает контейнер: он лежит в томе профилей."""
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\t".join([stamp, *(str(f) for f in fields)]) + "\n")


def reset_pages(browser) -> None:
    """После сбоя вкладка остаётся на /sorry/ или на выдаче. Чистим до about:blank.

    Без этого следующий поиск уходит в context.new_page() — тот самый вызов,
    который однажды подвесил воркер на 36 минут.
    """
    try:
        context = browser._ensure_browser()
        for page in context.pages[1:]:
            page.close()
        if context.pages:
            context.pages[0].goto("about:blank", wait_until="domcontentloaded")
    except Exception as error:
        print(f"  не смог прибрать вкладки: {type(error).__name__}", flush=True)


def ask(call, *args) -> tuple[bool, str, bool]:
    """Один вопрос: (получен ответ, что вышло, это /sorry/)."""
    from src.worker.browser import BlockedError

    start = time.monotonic()
    try:
        length = call(*args)
    except BlockedError:
        return False, f"/sorry/ за {time.monotonic() - start:.0f}с", True
    except Exception as error:
        name = "отказ Google" if "wait_for_function" in str(error) else type(error).__name__
        # первая строка сообщения — там Playwright пишет, какой именно вызов
        # не дождался; без неё TimeoutError неотличим от TimeoutError
        detail = str(error).strip().splitlines()[0][:120]
        return False, f"{name} за {time.monotonic() - start:.0f}с: {detail}", False
    return True, f"ответ {length} симв. за {time.monotonic() - start:.0f}с", False


def round_once(browser, question: str, followup: str) -> tuple[bool, str, bool, str, bool]:
    """Поиск и продолжение диалога по одной теме."""
    session: list[str] = []

    def search(text):
        session.append(browser.new_search_session("warm", text))
        return len(browser.get_last_ai_answer("warm", session[0]))

    def continue_talk(text):
        browser.ask_followup("warm", session[0], text)
        return len(browser.get_last_ai_answer("warm", session[0]))

    first_ok, first, blocked = ask(search, question)
    if not first_ok:
        return False, first, False, "—", blocked

    second_ok, second, blocked = ask(continue_talk, followup)
    if second_ok:
        try:
            browser.close_session("warm", session[0])
        except Exception:
            pass
    return True, first, second_ok, second, blocked


def main() -> None:
    parser = argparse.ArgumentParser(description="Прогрев профиля разнотемным потоком поисков")
    parser.add_argument("profile", help="имя профиля в пуле, например user1")
    parser.add_argument("--rounds", type=int, default=len(TOPICS),
                        help=f"сколько поисков сделать (по умолчанию {len(TOPICS)})")
    args = parser.parse_args()

    root = Path(os.getenv("AIFARM_PROFILE_ROOT", "/data/profiles"))
    profile = root / args.profile
    profile.mkdir(parents=True, exist_ok=True)
    os.environ["AIFARM_PROFILE_PATH"] = str(profile)
    log = root / "warm_log.tsv"

    from src.worker.browser import Browser

    def on_deadline(_signum, _frame):
        raise RoundTimeout()

    signal.signal(signal.SIGALRM, on_deadline)

    browser = Browser(worker_id="warm")
    topics = TOPICS[:]
    random.shuffle(topics)
    done = first_ok = follow_ok = 0

    print(f"прогрев {args.profile}, {args.rounds} поисков, лог в {log}", flush=True)
    print("капчу решай в окне — скрипт её переждёт и пойдёт дальше\n", flush=True)
    try:
        for index in range(args.rounds):
            question, followup = topics[index % len(topics)]
            print(f"--- {index + 1}/{args.rounds} {question!r}", flush=True)

            signal.alarm(ROUND_DEADLINE_S)
            try:
                got_first, first, got_second, second, blocked = round_once(
                    browser, question, followup
                )
            except RoundTimeout:
                got_first = got_second = blocked = False
                first = f"РАУНД ЗАВИС, снимаю браузер (>{ROUND_DEADLINE_S}с)"
                second = "—"
                try:
                    browser.shutdown()
                except Exception:
                    pass
                browser = Browser(worker_id="warm")
            finally:
                signal.alarm(0)
            print(f"    первый:  {first}\n    второй:  {second}", flush=True)
            log_line(log, args.profile, index + 1, question, first, second)
            done += 1
            first_ok += got_first
            follow_ok += got_second

            if not (got_first and got_second):
                reset_pages(browser)  # вкладка осталась на сбое, следующий поиск ей помешает
            if blocked:
                print(f"    капча — жду {CAPTCHA_PAUSE_S:.0f}с, реши её в окне\n", flush=True)
                time.sleep(CAPTCHA_PAUSE_S)
                continue

            if index + 1 < args.rounds:
                gap = GAP_S * random.uniform(0.6, 1.4)
                print(f"    пауза {gap:.0f}с\n", flush=True)
                time.sleep(gap)
    except KeyboardInterrupt:
        print("\nостановлено вручную", flush=True)
    finally:
        try:
            browser.shutdown()
        except Exception:
            pass

    print("\n" + "=" * 60)
    print(f"{args.profile}: попыток {done}, первых ответов {first_ok}, "
          f"продолжений диалога {follow_ok}")
    if follow_ok:
        print("продолжение диалога работает — датацентровый адрес нас не режет")
    else:
        print("продолжений нет ни одного: профиль тут ни при чём, режет адрес")


if __name__ == "__main__":
    main()
