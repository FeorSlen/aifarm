#!/usr/bin/env python3
"""Один профиль — один поисковый запрос, дальше окно принадлежит человеку.

Скрипт делает ровно четыре вещи:
  1. поднимает Camoufox на указанном профиле (при первом запуске пишет
     device.json — постоянное железо личности);
  2. набирает ОДИН поисковый запрос на google.com и листает выдачу;
  3. отдаёт окно и больше в него не лезет: никаких вторых вопросов, кликов,
     навигации. Заходи в «Режим ИИ», спрашивай что хочешь, решай капчу — скрипт
     только ждёт, пока ты закроешь окно;
  4. по закрытию печатает, что осело в куках.

Куки скрипт не трогает вообще: инъекция выключена (AIFARM_SEED_COOKIES=false),
файл cookies.sqlite читается только после закрытия браузера.

    .venv/bin/python <этот файл> user1
    .venv/bin/python <этот файл> user2 --query "свой запрос"
"""

import argparse
import os
import random
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = next(
    (d for d in [Path.cwd(), *Path.cwd().parents] if (d / "src" / "worker" / "browser.py").exists()),
    None,
)
if PROJECT_ROOT is None:
    sys.exit("Запускай из каталога проекта aifarm: src/worker/browser.py рядом не найден.")
PROFILE_ROOT = PROJECT_ROOT / "protected profile" / "profile"

# Флаги читаются модулем browser на импорте — выставляем до него.
os.environ["GOOGLE_HEADLESS"] = "false"
os.environ["AIFARM_SEED_COOKIES"] = "false"   # никакого подставного SOCS
os.environ["AIFARM_ACCEPT_CONSENT"] = "true"  # диалог согласия — не капча, кликается сам
os.environ.setdefault("AIFARM_READ_SCROLL", "true")

sys.path.insert(0, str(PROJECT_ROOT))

from src.worker.browser import GOOGLE_URL, SEARCH_BOX, Browser  # noqa: E402

QUERIES = [
    "погода на выходные", "как хранить базилик чтобы не вял", "во сколько закрывается почта",
    "чем отличается ssd от nvme", "рецепт шакшуки", "курс евро", "расписание электричек",
    "сколько варить булгур", "когда сажать чеснок под зиму", "как убрать пятно от кофе с дивана",
    "что такое ипотечные каникулы", "как научиться подтягиваться с нуля", "почему ноутбук шумит",
    "сколько лететь до стамбула", "как правильно заваривать зелёный чай",
]


def clear_block_marker(profile: Path) -> None:
    """Снимает метку /sorry/: прогретый профиль должен сразу вернуться в пул.

    Воркер держит заблокированный профиль вне ротации, пока не истечёт
    AIFARM_PROFILE_COOLDOWN_H; после ручного прогрева ждать незачем.
    """
    marker = profile / ".needs_attention"
    if marker.exists():
        marker.unlink()
        print(f"{profile.name}: метка блокировки снята, профиль снова в пуле", flush=True)


def show_cookies(profile: Path) -> None:
    """Что осело в профиле. Печатается всегда, чем бы прогон ни кончился."""
    print(f"\n--- {profile.name} ---", flush=True)
    device = profile / "device.json"
    print(f"device.json: {'есть' if device.exists() else 'НЕТ'}", flush=True)
    db = profile / "cookies.sqlite"
    if not db.exists():
        print("куки: cookies.sqlite не появился", flush=True)
        return
    try:
        con = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
        rows = con.execute("SELECT host, name FROM moz_cookies ORDER BY host, name").fetchall()
        con.close()
    except sqlite3.Error as error:
        print(f"куки: cookies.sqlite не читается ({error})", flush=True)
        return
    print(f"куки: {len(rows)}", flush=True)
    for host, name in rows:
        print(f"   {host:26} {name}", flush=True)


def browser_pids(profile: Path) -> list[str]:
    """Живые процессы camoufox, открытые на этом профиле."""
    try:
        return subprocess.run(
            ["pgrep", "-f", f"camoufox-bin.*-profile {profile}"], capture_output=True, text=True
        ).stdout.split()
    except FileNotFoundError:  # в slim-образе procps может не быть
        print("pgrep недоступен: слежу за окном по вкладкам, не по процессу", flush=True)
        return []


def free_profile(profile: Path) -> None:
    """Снимает всё, что мешает Firefox открыть профиль.

    Брошенный браузер от прерванного прогона держит -profile и после смерти
    питона: следующая попытка открыть тот же профиль молча выходит с кодом 0,
    и Playwright показывает это как «Failed to launch the browser process» —
    даже если каталог профиля успели удалить и создать заново.
    """
    orphans = browser_pids(profile)
    for pid in orphans:
        print(f"добиваю брошенный браузер {pid}", flush=True)
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, ValueError):
            continue
    if orphans:
        time.sleep(3)
    lock = profile / "lock"
    if lock.is_symlink():
        pid = os.readlink(lock).rpartition("+")[2]
        if pid.isdigit() and Path(f"/proc/{pid}").exists():
            sys.exit(f"{profile.name}: профиль занят живым процессом {pid}.")
        lock.unlink(missing_ok=True)
        (profile / ".parentlock").unlink(missing_ok=True)
        print(f"снят замок от мёртвого процесса {pid}", flush=True)


def hand_over(profile: Path, name: str, limit: float) -> None:
    """Окно отдано человеку: ждём смерти процесса браузера, ничего не трогая.

    Смотрим на процесс ОС, а не на вкладки Playwright: о закрытии руками драйвер
    узнаёт, только если его о чём-то спросить, и ожидание по вкладкам висит
    вечно (видели на user2 и user3). Открывай вкладки, ходи в AI Mode, решай
    капчу — скрипт очнётся, когда процесс браузера исчезнет.
    """
    print(f"\n{name}: запрос отработан. ОКНО ТВОЁ — заходи в «Режим ИИ», спрашивай,\n"
          f"решай капчу. Скрипт больше в него не лезет. Закрой браузер, когда закончишь\n"
          f"(жду до {limit / 60:.0f} мин).", flush=True)
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if not browser_pids(profile):
            break
        time.sleep(2)
    print(f"{name}: браузер закрыт", flush=True)


def _quiet_shutdown(browser) -> None:
    try:
        browser.shutdown()
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("profile", help="имя профиля, например user1")
    parser.add_argument("--query", help="свой запрос вместо случайного из списка")
    parser.add_argument("--wait", type=float, default=3600, help="потолок ожидания, с")
    parser.add_argument("--root", help="каталог профилей (на проде /data/profiles)")
    args = parser.parse_args()

    if not os.environ.get("DISPLAY"):
        sys.exit("DISPLAY пуст — окно показывать некуда.")
    profile = (Path(args.root) if args.root else PROFILE_ROOT) / args.profile
    profile.mkdir(parents=True, exist_ok=True)
    free_profile(profile)
    query = args.query or random.choice(QUERIES)

    os.environ["AIFARM_PROFILE_PATH"] = str(profile)
    print(f"профиль: {profile}\nпрокси: {os.getenv('PROXY_URL') or 'нет, прямое подключение'}\n"
          f"запрос: {query}\n", flush=True)

    browser = Browser(worker_id="warm")
    try:
        context = browser._ensure_browser()
        page = browser._take_free_page(context)
        page.bring_to_front()
        page.goto(GOOGLE_URL, wait_until="domcontentloaded", timeout=45000)
        browser._accept_cookies(page)

        box = page.locator(SEARCH_BOX)
        browser._type_query(page, box, query)
        page.wait_for_timeout(random.uniform(400, 1100))
        box.press("Enter")
        page.wait_for_load_state("domcontentloaded", timeout=45000)
        page.wait_for_timeout(random.uniform(1200, 2500))
        if "/sorry/" in page.url:
            print("Google показал проверку — реши капчу в окне, дальше окно твоё", flush=True)
        else:
            print(f"выдача: {page.locator('a:has(h3)').count()} результатов", flush=True)
            browser._read_scroll(page, steps=random.randint(2, 4))  # почитать выдачу

        hand_over(profile, args.profile, args.wait)
    except Exception as error:
        if "has been closed" in str(error) or error.__class__.__name__ == "TargetClosedError":
            print(f"{args.profile}: окно закрыто", flush=True)
        else:
            print(f"{args.profile}: сбой — {error.__class__.__name__}: {error}", flush=True)
    finally:
        # Закрытие уже мёртвого браузера умеет висеть вечно, поэтому уводим его в
        # отдельный поток и не ждём дольше 15 с: отчёт о куках важнее, а файл к
        # этому моменту уже сброшен закрытым браузером.
        closing = threading.Thread(target=_quiet_shutdown, args=(browser,), daemon=True)
        closing.start()
        closing.join(15)
        if closing.is_alive():
            print("браузер не закрылся сам, оставляю его процессу ОС", flush=True)
        show_cookies(profile)  # печатается при любом исходе
        clear_block_marker(profile)
        sys.stdout.flush()
        os._exit(0)  # не даём подвисшему драйверу держать процесс


if __name__ == "__main__":
    main()
