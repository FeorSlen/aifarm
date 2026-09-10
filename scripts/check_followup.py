#!/usr/bin/env python3
"""Проверка профилей пула на продолжение диалога в AI Mode.

Запуск внутри контейнера воркера — там уже поднят Xvfb :99:

    docker compose stop worker                       # снять блокировку профилей
    docker compose run --rm -e DISPLAY=:99 worker \
        sh -c 'Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp & python scripts/check_followup.py'

Маркеры .needs_attention скрипт не ставит: это диагностика, а не бой.
"""

import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Пауза между профилями: пять поисков подряд с одного адреса — сама по себе
# заявка на капчу, а мы как раз репутацию адреса и меряем.
PROBE_GAP_S = float(os.getenv("AIFARM_PROBE_GAP_S", "90"))

# Свой вопрос на каждый профиль, и на каждый прогон новый: один и тот же запрос
# с десяти личностей — сам по себе повод показать нам /sorry/.
PROBES = [
    ("кто разработал игру Tunic", "а сколько она проходится"),
    ("что такое игра Islets", "а на что она похожа"),
    ("чем известна Ender Lilies", "а какая там музыка"),
    ("что за игра Grime метроидвания", "а насколько она сложная"),
    ("кто издал игру Afterimage", "а сколько там локаций"),
    ("что такое Death's Gambit Afterlife", "а чем отличается от оригинала"),
    ("чем известна игра Haiku the Robot", "а большой ли там мир"),
    ("что за игра Prince of Persia Lost Crown", "а есть ли там секреты"),
    ("что такое Ori and the Will of the Wisps", "а сложные ли там боссы"),
    ("кто сделал игру Blasphemous 2", "а чем она отличается от первой"),
    ("что за игра Axiom Verge", "а стоит ли играть во вторую часть"),
    ("чем известна Environmental Station Alpha", "а долго ли её проходить"),
    ("что такое игра Rabi-Ribi", "а есть ли там сложные боссы"),
    ("кто разработал Momodora Moonlit Farewell", "а связана ли она с прошлыми частями"),
    ("что за игра Vernal Edge", "а какая там боевая система"),
    ("чем известна Bo Path of the Teal Lotus", "а сколько она идёт"),
    ("что такое игра Ultros", "а кто рисовал ей графику"),
    ("что за игра Aeterna Noctis", "а насколько она длинная"),
    ("чем известна Record of Lodoss War Deedlit", "а сколько в ней часов"),
    ("что такое игра Supraland", "а нужна ли там смекалка"),
]


def pick_probes(count: int) -> list[tuple[str, str]]:
    """Неповторяющиеся вопросы на прогон, порядок случайный."""
    pool: list[tuple[str, str]] = []
    while len(pool) < count:
        batch = PROBES[:]
        random.shuffle(batch)
        pool += batch
    return pool[:count]


def check(profile_dir: str, question: str, followup: str) -> str:
    """Один профиль: первый вопрос, затем продолжение диалога."""
    os.environ["AIFARM_PROFILE_PATH"] = profile_dir  # читается в Browser._new_identity
    from src.worker.browser import BlockedError, Browser

    browser = Browser(worker_id="check")
    try:
        start = time.monotonic()
        session = browser.new_search_session("check", question)
        first = browser.get_last_ai_answer("check", session)
        first_time = time.monotonic() - start

        start = time.monotonic()
        browser.ask_followup("check", session, followup)
        second = browser.get_last_ai_answer("check", session)
        return (f"ДЕРЖИТ ФОЛЛОУ-АП (первый {first_time:.0f} с / {len(first)} симв., "
                f"второй {time.monotonic() - start:.0f} с / {len(second)} симв.)")
    except BlockedError:
        return "/sorry/ — профиль под капчей"
    except Exception as error:
        text = str(error)
        if "wait_for_function" in text:
            return "фоллоу-ап отказан (Something went wrong)"
        return f"{type(error).__name__}: {text[:90]}"
    finally:
        try:
            browser.shutdown()
        except Exception:
            pass


def main() -> None:
    root = os.getenv("AIFARM_PROFILE_ROOT", "/data/profiles")
    names = sys.argv[1:] or sorted(
        (entry for entry in os.listdir(root) if entry.startswith("user")),
        key=lambda name: int(name[4:] or 0),
    )
    print(f"каталог профилей: {root}\n", flush=True)

    results = []
    probes = pick_probes(len(names))
    for index, name in enumerate(names):
        if index:
            gap = PROBE_GAP_S * random.uniform(0.6, 1.4)
            print(f"    пауза {gap:.0f} с перед следующим профилем\n", flush=True)
            time.sleep(gap)
        question, followup = probes[index]
        print(f"--- {name}: {question!r}", flush=True)
        verdict = check(os.path.join(root, name), question, followup)
        print(f"    {verdict}\n", flush=True)
        results.append((name, verdict))

    print("=" * 64)
    for name, verdict in results:
        print(f"{name:8} {verdict}")
    alive = [name for name, verdict in results if "ДЕРЖИТ" in verdict]
    print("=" * 64)
    print(f"держат фоллоу-ап: {', '.join(alive) if alive else 'ни один'}")


if __name__ == "__main__":
    main()
