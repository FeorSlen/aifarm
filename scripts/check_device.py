#!/usr/bin/env python3
"""Проверка постоянства устройства профиля между запусками браузера.

Google не трогается: страница — about:blank. Проверяются две вещи.

  1. Профиль с сохранённым device.json отдаёт одно и то же устройство дважды.
  2. Профиль без него отдаёт разные — то есть Camoufox по умолчанию и правда
     выдумывает новую машину на каждый запуск, а файл эту дыру закрывает.

Запуск внутри контейнера воркера:

    docker compose run --rm -e DISPLAY=:99 worker \
        sh -c 'Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp & python scripts/check_device.py'
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Холст берём в пробу намеренно: canvas:seed — это то, чем машина отличается от
# машины, и именно он раньше менялся на каждом запуске.
PROBE = """
() => {
  // Хэшируем ВЕСЬ data-URL: хвост у PNG — набивка, по нему разные машины
  // выглядят одинаково. Рисунок нарочно неровный, чтобы шум canvas:seed попал
  // в пиксели, а не потерялся на плоской заливке.
  const digest = (text) => {
    let hash = 0;
    for (let i = 0; i < text.length; i++) hash = (hash * 31 + text.charCodeAt(i)) | 0;
    return hash;
  };
  const canvas = document.createElement('canvas');
  canvas.width = 240;
  canvas.height = 60;
  const ctx = canvas.getContext('2d');
  const gradient = ctx.createLinearGradient(0, 0, 240, 60);
  gradient.addColorStop(0, '#f0a');
  gradient.addColorStop(1, '#0af');
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, 240, 60);
  ctx.font = '17px "Times New Roman"';
  ctx.fillStyle = 'rgba(20,80,40,0.7)';
  ctx.fillText('aifarm \u2014 \u0442\u0435\u0441\u0442 \u2603', 4, 40);
  ctx.beginPath();
  ctx.arc(200, 30, 22, 0, Math.PI * 1.7);
  ctx.stroke();

  const gl = document.createElement('canvas').getContext('webgl');
  const info = gl && gl.getExtension('WEBGL_debug_renderer_info');

  return {
    ua: navigator.userAgent,
    platform: navigator.platform,
    cores: navigator.hardwareConcurrency,
    screen: `${screen.width}x${screen.height}`,
    canvas: digest(canvas.toDataURL()),
    webgl: info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : 'n/a',
  };
}
"""


def probe(profile_dir: str) -> dict:
    """Один запуск браузера на профиле: снимаем устройство и закрываемся."""
    os.environ["AIFARM_PROFILE_PATH"] = profile_dir
    from src.worker.browser import Browser

    browser = Browser(worker_id="device-check")
    try:
        context = browser._ensure_browser()
        page = browser._take_free_page(context)
        return page.evaluate(PROBE)
    finally:
        browser.shutdown()


def report(title: str, first: dict, second: dict, *, expect_same: bool) -> bool:
    same = first == second
    verdict = "совпало" if same else "разошлось"
    print(f"\n--- {title}: {verdict}")
    for key in first:
        mark = " " if first[key] == second[key] else "!"
        print(f"  {mark} {key:9} {first[key]}")
        if first[key] != second[key]:
            print(f"  {' ':9}   {second[key]}")
    ok = same is expect_same
    print("  ИТОГ:", "как и ожидалось" if ok else "НЕ ТАК, КАК ОЖИДАЛОСЬ")
    return ok


def main() -> None:
    from src.worker.browser import DEVICE_FILE

    with tempfile.TemporaryDirectory() as root:
        pinned = str(Path(root) / "pinned")
        loose = str(Path(root) / "loose")

        first = probe(pinned)
        saved = json.loads((Path(pinned) / DEVICE_FILE).read_text())
        print(f"устройство закреплено, ключей: {len(saved)}")
        pinned_ok = report("профиль с device.json", first, probe(pinned), expect_same=True)

        # контроль: сносим файл перед вторым запуском — устройство обязано уплыть
        first_loose = probe(loose)
        (Path(loose) / DEVICE_FILE).unlink()
        loose_ok = report("профиль без device.json", first_loose, probe(loose), expect_same=False)

    print("\n" + "=" * 60)
    if pinned_ok and loose_ok:
        print("устройство держится за профилем, без файла — плывёт. Правка работает.")
        return
    print("проверка провалена, деплоить нельзя")
    sys.exit(1)


if __name__ == "__main__":
    main()
