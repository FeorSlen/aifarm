#!/usr/bin/env python3
"""Почему Locator.click не доходит до поля поиска.

Три шага, от дешёвого к дорогому:

  1. локальная страница (data:) — клик вообще работает в этой сборке?
  2. google.com — где на самом деле лежит textarea[name=q], видно ли его,
     не накрыто ли оно чем-то, совпадают ли размеры окна с подставными;
  3. то же самое с выключенным humanize — он единственный, кто трогает курсор.

Google дёргается ровно дважды. Запуск внутри контейнера воркера:

    docker compose run --rm -e DISPLAY=:99 worker \
        sh -c 'Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp & sleep 1; \
               python scripts/check_click.py'
"""

import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LOCAL_PAGE = (
    "data:text/html,<html><body style='margin:0;background:%23eee'>"
    "<input id=q style='position:absolute;left:300px;top:380px;width:420px;height:44px'>"
    "</body></html>"
)

# Где элемент лежит, видно ли его и что находится в его середине. Если
# elementFromPoint возвращает не его — элемент чем-то накрыт, и Playwright
# будет ждать актабельности ровно до таймаута.
GEOMETRY = """
(selector) => {
  const el = document.querySelector(selector);
  if (!el) return {found: false, title: document.title, url: location.href};
  const r = el.getBoundingClientRect();
  const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
  const top = document.elementFromPoint(cx, cy);
  return {
    found: true,
    url: location.href,
    rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
    inViewport: r.x >= 0 && r.y >= 0 && r.right <= innerWidth && r.bottom <= innerHeight,
    covered: top !== el,
    coveredBy: top === el ? null : (top ? top.tagName + '.' + (top.className || '') : 'null'),
    inner: [innerWidth, innerHeight],
    outer: [outerWidth, outerHeight],
    screenXY: [screenX, screenY],
  };
}
"""


def open_browser(profile: Path, humanize: bool):
    profile.mkdir(parents=True, exist_ok=True)
    os.environ["AIFARM_PROFILE_PATH"] = str(profile)
    from src.worker import browser as module

    module._BASE_CONFIG["humanize"] = humanize  # флаг читается при импорте
    return module.Browser(worker_id="click-check")


def try_click(page, selector: str) -> str:
    start = time.monotonic()
    try:
        page.locator(selector).click(timeout=20000)
        return f"клик прошёл за {time.monotonic() - start:.0f}с"
    except Exception as error:
        return f"НЕ КЛИКНУЛОСЬ за {time.monotonic() - start:.0f}с: {str(error).splitlines()[0][:80]}"


def step(title: str, profile: Path, humanize: bool, url: str, selector: str) -> None:
    print(f"\n--- {title}", flush=True)
    browser = open_browser(profile, humanize)
    try:
        page = browser._take_free_page(browser._ensure_browser())
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        if "google" in url:
            browser._accept_cookies(page)
            page.wait_for_timeout(1500)
        info = page.evaluate(GEOMETRY, selector)
        if not info.get("found"):
            print(f"    элемента {selector} на странице НЕТ; url={info['url']}", flush=True)
        else:
            print(f"    элемент {info['rect']}, во вьюпорте: {info['inViewport']}, "
                  f"накрыт: {info['covered']}{' -> ' + str(info['coveredBy']) if info['covered'] else ''}",
                  flush=True)
            print(f"    окно inner={info['inner']} outer={info['outer']} "
                  f"screenXY={info['screenXY']}", flush=True)
        print(f"    {try_click(page, selector)}", flush=True)
    except Exception as error:
        print(f"    шаг сорвался: {type(error).__name__}: {str(error).splitlines()[0][:100]}", flush=True)
    finally:
        try:
            browser.shutdown()
        except Exception:
            pass


def main() -> None:
    source = Path("/data/profiles/user1/device.json")
    root = Path("/tmp/clickcheck")
    shutil.rmtree(root, ignore_errors=True)

    for name in ("local", "google", "google_nohuman"):
        profile = root / name
        profile.mkdir(parents=True)
        if source.exists():  # то же устройство, что и у боевого профиля
            shutil.copy(source, profile / "device.json")

    step("локальная страница, humanize вкл", root / "local", True, LOCAL_PAGE, "#q")
    step("google.com, humanize вкл", root / "google", True,
         "https://www.google.com", "textarea[name='q']")
    step("google.com, humanize ВЫКЛ", root / "google_nohuman", False,
         "https://www.google.com", "textarea[name='q']")


if __name__ == "__main__":
    main()
