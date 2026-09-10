#!/usr/bin/env python3
"""Open one persistent browser profile for manual warm-up."""

import argparse
import os
import sys
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROFILE_ROOT = PROJECT_ROOT / "protected profile" / "profile"

os.environ["GOOGLE_HEADLESS"] = "false"
os.environ["AIFARM_SEED_COOKIES"] = "false"
os.environ["AIFARM_ACCEPT_CONSENT"] = "true"

sys.path.insert(0, str(PROJECT_ROOT))

from src.worker.browser import Browser, GOOGLE_URL  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Open a persistent test profile")
    parser.add_argument("profile", type=int, choices=range(1, 11))
    args = parser.parse_args()

    profile_id = f"user{args.profile}"
    profile_path = PROFILE_ROOT / profile_id
    profile_path.mkdir(parents=True, exist_ok=True)
    os.environ["AIFARM_PROFILE_PATH"] = str(profile_path)

    browser = Browser(worker_id="warm")
    try:
        context = browser._ensure_browser()
        page = browser._take_free_page(context)
        page.bring_to_front()
        page.goto(GOOGLE_URL, wait_until="domcontentloaded")
        browser._accept_cookies(page)
        print(f"Открыт {profile_id}. Используй браузер вручную.", flush=True)
        input("Нажми Enter здесь, чтобы корректно закрыть браузер...")
    finally:
        try:
            browser.shutdown()
        except PlaywrightError:
            print("Окно уже закрыто; данные профиля оставлены на диске.", flush=True)

    ready = input("Проверка пройдена, вернуть профиль в пул? [y/N]: ").strip().lower()
    if ready in {"y", "yes", "д", "да"}:
        (profile_path / ".needs_attention").unlink(missing_ok=True)
        print(f"{profile_id} готов и возвращён в пул.", flush=True)
    else:
        print(f"{profile_id} сохранён, статус пула не изменён.", flush=True)


if __name__ == "__main__":
    main()
