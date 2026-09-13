"""
Контракт с main:

    br = Browser(worker_id="worker1")
    session_id = br.new_search_session(user_session_id, query) -> str
    br.ask_followup(user_session_id, session_id, query)
    text = br.get_last_ai_answer(user_session_id, session_id)
    sources = br.last_sources(user_session_id, session_id)

Один Browser = один живой браузер/профиль/куки на всё время жизни текущей
"личности". Личность — это каталог профиля на диске (куки + постоянное
устройство в device.json) плюс прокси. Устройство генерируется при первом
запуске профиля и дальше только читается: без этого Camoufox выдаёт новую
машину на каждый запуск, и один cookie jar возвращается то с Windows, то с
macOS.

Профиль сменяется в двух случаях: его спалили (BlockedError -> burn_and_rotate,
маркер со временем блокировки, возврат в ротацию через PROFILE_COOLDOWN) либо
он отработал норму вопросов (PROFILE_MAX_QUERIES -> _rotate_by_load, без
маркера). Ничего не удаляется. Сам объект Browser переживает смену, но
session_id старой личности после неё недействительны.
"""

import base64
import json
import math
import os
import random
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from camoufox.sync_api import Camoufox
from camoufox.utils import launch_options as camoufox_launch_options
from playwright._impl._errors import TargetClosedError


def _google_url(locale: str | None) -> str:
    """Открывает Search на том же языке, что и fingerprint браузера."""
    if not locale:
        return "https://www.google.com"
    language = locale.partition("-")[0].lower()
    if not re.fullmatch(r"[a-z]{2,3}", language):
        raise ValueError("AIFARM_LOCALE must start with an ISO language code")
    return f"https://www.google.com/?hl={language}"


BROWSER_LOCALE = os.getenv("AIFARM_LOCALE", "").strip() or None
GOOGLE_URL = _google_url(BROWSER_LOCALE)
SEARCH_BOX = "textarea[name='q']"
ACCEPT_COOKIES_BTN = "#L2AGLb"  # id кнопки "Accept all" у Google, не зависит от локали
AI_MODE_LINK = 'a[href*="udm=50"]'  # вкладка "Режим ИИ" на странице выдачи; udm=50 от локали не зависит
# Поле уточняющего вопроса в AI Mode. На странице AI Mode ДВЕ textarea: скрытая
# форма отзыва (class e3RQ5d, placeholder 'Jaa tietoja…' / 'Поделиться…') идёт в
# DOM первой, настоящее поле диалога (class ITIRGe) — второй. Фильтр по
# видимости обязателен: без него .first цепляет скрытую форму и ждёт её
# видимости вечно (наблюдали Timeout с "43 × locator resolved").
# Placeholder не используем — он меняется с языком интерфейса, а язык задаёт
# exit-IP (видели французский с VPS и финский через мобильный прокси).
FOLLOWUP_INPUT = 'textarea.ITIRGe:visible, textarea:not([name="q"]):visible'
AI_ANSWER = '[data-container-id="main-col"]'  # один такой блок на ход диалога AI Mode
# Блок «контент не сгенерирован», который Google рисует вместо ответа. Атрибут
# ставит его же скрипт (`d.setAttribute("data-aeie","true")`), поэтому маркер не
# зависит от языка — сам текст внутри локализован (видели французский на проде).
ANSWER_ERROR = '[data-aeie="true"]'

# Физические клавиши русской раскладки в английской: уточнение «Почему»
# отправляется как «Gjxtve». Остальные символы оставляем как есть.
_RU_TO_EN_KEYS = str.maketrans(
    "ёйцукенгшщзхъфывапролджэячсмитьбю"
    "ЁЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ",
    "`qwertyuiop[]asdfghjkl;'zxcvbnm,."
    "~QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>",
)

PROFILES_DIR = os.getenv("PROFILES_DIR", "./profiles")
SESSION_TTL = timedelta(hours=24)

# Профиль, на котором Google показал /sorry/, не удаляется и не выбывает из пула
# навсегда: в него кладётся маркер со временем блокировки, и через COOLDOWN он
# сам возвращается в ротацию. Пустой/битый маркер (например, поставленный
# руками) держит профиль вне пула до ручного снятия — scripts/warm_profile.py.
BLOCK_MARKER = ".needs_attention"
PROFILE_COOLDOWN = timedelta(hours=float(os.getenv("AIFARM_PROFILE_COOLDOWN_H", "6")))

# Профиль отрабатывает норму вопросов и уступает место следующему, не дожидаясь
# /sorry/: ровный поток с одного профиля — сам по себе повод для проверки.
# Порог берётся с разбросом, чтобы смена не приходилась всегда на круглое число.
PROFILE_MAX_QUERIES = int(os.getenv("AIFARM_PROFILE_MAX_QUERIES", "12"))  # 0 = не ротировать по нагрузке

# Устройство профиля (fingerprint) генерируется один раз и лежит рядом с куками.
# Без этого файла Camoufox выдаёт НОВОЕ устройство на каждый запуск (в
# launch_options fingerprint=None и os=None => "infinite unique fingerprints"),
# то есть один и тот же cookie jar возвращается то с Windows, то с macOS — для
# Google это противоречие внутри одной личности.
DEVICE_FILE = "device.json"

# Что в устройство НЕ сохраняем. География обязана каждый запуск выводиться из
# текущего адреса, иначе профиль противоречит своему же IP; addons/humanize —
# наши настройки, а не свойства машины; history.length у живой сессии и должен
# плавать. Всё остальное из CAMOU_CONFIG — это железо, и оно постоянно.
VOLATILE_CONFIG_KEYS = (
    "geolocation:", "locale:", "timezone", "addons", "humanize", "window.history.length",
)

# Таймауты рассчитаны на медленный релей через прокси (residential/mobile).
# Все — потолки, срабатывают только при зависании; на живом соединении не мешают.
NAV_TIMEOUT = int(os.getenv("NAV_TIMEOUT_MS", "45000"))   # goto / load-state
EL_TIMEOUT = int(os.getenv("EL_TIMEOUT_MS", "20000"))     # ожидание элементов (AI Mode, поле, ответ)
STREAM_TIMEOUT = int(os.getenv("STREAM_TIMEOUT_S", "45"))  # стриминг ответа AI Mode
# Появление НОВОГО блока ответа на уточняющий вопрос. Отдельно от EL_TIMEOUT:
# тот про элементы, которые уже есть в DOM, а здесь Google сперва думает над
# вопросом и только потом создаёт контейнер — на длинном вопросе 20 с не хватает.
ANSWER_START_TIMEOUT = int(os.getenv("ANSWER_START_TIMEOUT_S", "60")) * 1000
# Проба «перекрыто ли поле поиска диалогом согласия». Это не ожидание элемента,
# а вопрос к уже загруженной странице, поэтому потолок свой и небольшой.
CONSENT_PROBE_TIMEOUT = int(os.getenv("CONSENT_PROBE_TIMEOUT_MS", "8000"))
# Потолок на клик по полю ввода. Меньше EL_TIMEOUT намеренно: клик по полю —
# не ожидание элемента, а необязательный жест, у которого есть замена (см.
# _focus_field). Выстаивать за него 20 с и валить сессию нельзя.
FIELD_CLICK_TIMEOUT = int(os.getenv("FIELD_CLICK_TIMEOUT_MS", "6000"))

# Сколько последних символов запроса допечатываются посимвольно. Остальное
# вставляется одним fill(): посимвольный ввод всей простыни — это по XHR в
# /complete/search почти на каждую клавишу и минуты равномерного стука, то
# есть прямая дорога на /sorry/. Человек длинный промпт вставляет, а не печатает.
HUMAN_TAIL_CHARS = 40

# Ритм печати. Равномерный интервал — главный машинный признак: у человека
# интервалы логнормальные (медиана внутри слова ~95 мс, длинный правый хвост),
# на границе слова и после пунктуации длиннее, изредка — настоящая пауза.
# Итог замера: медиана ~117 мс, среднее ~142 мс/символ — около 85 слов в минуту,
# темп быстрого, но живого человека. Быстрее гнать смысла нет: после перехода
# на fill() вручную набирается только хвост в 40 символов, это ~5 с на запрос,
# а ровный сверхбыстрый набор — сам по себе признак бота.
KEY_DELAY_MEDIAN_MS = 110
KEY_DELAY_SIGMA = 0.32      # разброс логнормали; 0 = машинно ровно
KEY_DELAY_MIN_MS = 45       # быстрее человек не успевает даже в накате
KEY_DELAY_MAX_MS = 400      # выше — это уже пауза, её добавляем отдельно
THINK_PAUSE_CHANCE = 0.02   # доля нажатий, перед которыми «задумался»

# Чтение ответа прокруткой. Ответ AI Mode длиннее экрана, и сессия, которая
# сгенерировала простыню и не сдвинула её ни на пиксель, ведёт себя не как
# читатель. Один жест = дистанция SCROLL_STEP_PX, но отправляется он НЕ одним
# wheel-событием: живой тачпад шлёт десятки мелких щелчков с разгоном в начале
# и торможением в конце. Одиночный прыжок на 300 px — это подпись автомата.
# Чтение ответа перед уточняющим вопросом. Раньше паузы не было вовсе: ответ
# признавался готовым через 1.5 с неизменности текста, и следующий ход начинал
# печататься через пару секунд — то есть «человек» осилил простыню на 3000
# знаков быстрее, чем успел бы её проскроллить. Считаем время по объёму
# прочитанного, с потолком, и проводим его не в мёртвом сне, а листая ответ.
FOLLOWUP_READ_S_PER_100 = (1.1, 2.3)  # секунд на 100 символов ответа
FOLLOWUP_READ_MIN_S = float(os.getenv("AIFARM_FOLLOWUP_READ_MIN_S", "5"))
FOLLOWUP_READ_MAX_S = float(os.getenv("AIFARM_FOLLOWUP_READ_MAX_S", "45"))

SCROLL_STEP_PX = (220, 520)     # дистанция одного жеста
SCROLL_PAUSE_MS = (350, 1100)   # пауза на чтение между жестами
SCROLL_TICK_PX = (8, 26)        # один щелчок внутри жеста
SCROLL_TICK_MS = (12, 34)       # пауза между щелчками


def _flag(name, default=True):
    return os.getenv(name, str(default)).strip().lower() not in ("0", "false", "no", "off", "")


# --- переключатели поведения (env, без ребилда; см. README) ---
# Человекоподобное движение курсора Camoufox. Потолок обязателен: при
# humanize=true без него курсор ведётся к цели «сколько понадобится», и мы
# своими глазами видели, как он ходит из стороны в сторону мимо строки поиска и
# не доходит вовсе. Цена этого — не косметика: Locator.click выстаивает весь
# таймаут и роняет сессию ("AI Mode не открылся"), а у page.mouse.move таймаута
# нет вообще, и воркер вставал на десятки минут. Camoufox принимает числом
# максимум секунд на одно движение — этого достаточно, поведение остаётся живым.
# AIFARM_HUMANIZE: false/0 — выключить совсем, число — свой потолок в секундах.
HUMANIZE_MAX_SECONDS = 1.5


def _humanize_setting():
    raw = os.getenv("AIFARM_HUMANIZE", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("", "1", "true", "yes", "on"):
        return HUMANIZE_MAX_SECONDS
    return float(raw)


FLAG_HUMANIZE = _humanize_setting()                   # человекоподобные движения курсора Camoufox
FLAG_GEOIP = _flag("AIFARM_GEOIP")                    # подгонка TZ/локали под exit-IP прокси
FLAG_BLOCK_WEBRTC = _flag("AIFARM_BLOCK_WEBRTC")      # блок утечки реального IP через WebRTC
FLAG_ACCEPT_CONSENT = _flag("AIFARM_ACCEPT_CONSENT")  # авто-принятие диалога согласия Google
FLAG_DENOISE = _flag("AIFARM_DENOISE")                # чистка ответа AI Mode от UI-обвязки
FLAG_SEED_COOKIES = _flag("AIFARM_SEED_COOKIES")      # инъекция куков (файл + дефолтное согласие)
FLAG_READ_SCROLL = _flag("AIFARM_READ_SCROLL")        # прокрутка ответа во время чтения

# Разбор ответа AI Mode на клоне ноды: сперва сноски, потом чистка от UI.
#
# Живой DOM трогать нельзя — на нём висит счётчик ходов ask_followup и скрипты
# Google. Кликов здесь нет намеренно: источники Google уже отрендерил ссылками
# внутри ответа, а лишние клики по чужому UI — это трафик на самом опасном для
# профиля пути. Если ссылок в ответе не оказалось, отдаём только текст, как раньше.
#
# Порядок обязателен: сноска сначала превращается в текстовый маркер [n] рядом с
# чипом и только потом чип выкидывается стрипом. Раньше стрип уносил 'sup'
# вместе со ссылкой, поэтому в ответе не оставалось ни одного источника.
#
# Список стрипа консервативный и структурный (роли/теги, не классы Google — те
# гниют за недели).
_ANSWER_JS = """
(el, options) => {
  const clone = el.cloneNode(true);
  const sources = [];
  const seen = new Map();

  // Адрес источника. Сноски AI Mode ведут на /goto?url=<непрозрачный токен>:
  // настоящего адреса в DOM нет нигде (проверено дампом живой страницы), но
  // ссылка публично редиректит на источник и без куков — поэтому отдаём её как
  // есть, а не выбрасываем вместе с остальными ссылками Google. Классический
  // /url?q=<цель> разворачиваем. Прочие ссылки Google — навигация по выдаче.
  const target = (href, depth) => {
    let url;
    try {
      url = new URL(href, location.href);
    } catch (error) {
      return null;
    }
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return null;
    if (/(^|\\.)google(\\.[a-z]{2,3})+$/.test(url.hostname)) {
      if (url.pathname === '/goto' && url.searchParams.get('url')) {
        url.hash = '';
        return url.href;
      }
      const inner = url.searchParams.get('q') || url.searchParams.get('url');
      return inner && depth < 3 ? target(inner, depth + 1) : null;
    }
    url.hash = '';
    return url.href;
  };

  for (const anchor of clone.querySelectorAll('a[href]')) {
    const url = target(anchor.getAttribute('href'), 0);
    if (!url) continue;
    let index = seen.get(url);
    if (index === undefined) {
      index = sources.length + 1;
      seen.set(url, index);
      sources.push({index: index, url: url, title: ''});
    }
    // Клон открепён от документа, поэтому только textContent: innerText на
    // открепённой ноде пуст. У сноски AI Mode текста нет вовсе, название
    // источника лежит в aria-label ("Python.org (+1) – Welcome to Python.org").
    const text = (anchor.textContent || '').trim();
    const label = text || (anchor.getAttribute('aria-label') || '').trim();
    const numeric = /^\\[?\\d+\\]?$/.test(label);
    if (!sources[index - 1].title && label && !numeric) {
      sources[index - 1].title = label.slice(0, 200);
    }
    // Пустой чип сноски и номерной sup уносим — текста в них нет. А вот чип с
    // текстом разворачиваем в этот самый текст: в AI Mode ссылками обёрнуты
    // сущности внутри предложения ("Tadej Pogačar" в кнопке-чипе), и стрип
    // кнопок вырезал их из ответа вместе с разметкой.
    const chip = anchor.closest('sup, button, [role="button"]');
    const node = chip || anchor;
    if (!node.parentNode) continue;
    node.parentNode.insertBefore(document.createTextNode(' [' + index + ']'), node.nextSibling);
    if (!text || (chip && numeric)) {
      node.remove();
    } else if (chip) {
      chip.replaceWith(document.createTextNode(text));
    }
  }

  for (const entry of sources) {
    if (entry.title) continue;
    try {
      entry.title = new URL(entry.url).hostname.replace(/^www\\./, '');
    } catch (error) {
      entry.title = '';
    }
  }

  if (options.denoise) {
    clone.querySelectorAll(
      'button, [role="button"], [role="menu"], [role="menuitem"], [role="dialog"],'
      + ' [role="listbox"], [role="tablist"], [role="navigation"], svg, script, style,'
      + ' [aria-hidden="true"], sup'
    ).forEach((n) => n.remove());
  }

  clone.style.position = 'fixed';
  clone.style.left = '-99999px';
  clone.style.top = '0';
  document.body.appendChild(clone);
  try {
    return {text: clone.innerText, sources: sources};
  } finally {
    clone.remove();
  }
}
"""


def _clean_text(text: str) -> str:
    """Тримит хвосты строк, схлопывает пустые в одну, убирает края.

    Отступы слева сохраняются (rstrip, не strip) — в ответах AI Mode бывает код
    и структурированный вывод. Чистая функция.
    """
    out: list[str] = []
    for line in (text or "").splitlines():
        line = line.rstrip()
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    return "\n".join(out).strip()


def _valid_sources(raw) -> list[dict]:
    """Отсеивает мусор из того, что вернула страница. Чистая функция.

    Всё, что приходит из браузера, — данные с чужой страницы: проверяем тип и
    схему каждого элемента, а не полагаемся на форму объекта из _ANSWER_JS.
    """
    sources: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        index, url, title = item.get("index"), item.get("url"), item.get("title")
        if not isinstance(index, int) or index < 1:
            continue
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        sources.append({
            "index": index,
            "url": url,
            "title": title.strip()[:200] if isinstance(title, str) else "",
        })
    return sources


def _followup_key_layout(text: str) -> str:
    """Набирает кириллицу теми же клавишами в английской раскладке."""
    return text.translate(_RU_TO_EN_KEYS)


def _ai_mode_url(search_url: str) -> str:
    """Добавляет AI Mode к текущей выдаче, сохраняя запрос и параметры Google."""
    parts = urlsplit(search_url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key != "udm"]
    query.append(("udm", "50"))
    return urlunsplit(parts._replace(query=urlencode(query)))

# Настройки, общие для любой личности (сам fingerprint-профиль/прокси —
# за экземпляром Browser, см. Browser._launch_config). fonts/screen/window=None —
# Camoufox сам берёт значения из реального распределения устройств, не трогаем.
# firefox_user_prefs пустой: любые не-дефолтные prefs — лишняя поверхность
# фингерпринта, а переход в AI Mode теперь по URL, а не кликом по ссылке.
_BASE_CONFIG = dict(
    geoip=FLAG_GEOIP,
    headless=os.getenv("GOOGLE_HEADLESS", "false").lower() == "true",
    block_webrtc=FLAG_BLOCK_WEBRTC,
    humanize=FLAG_HUMANIZE,
    persistent_context=True,
    fonts=None,
    screen=None,
    window=None,
    args=[],
    i_know_what_im_doing=False,
    firefox_user_prefs={
        "browser.link.open_newwindow": 3,
        "browser.link.open_newwindow.restriction": 0,
    },
)


class BlockedError(Exception):
    """Google принял браузер за бота (редирект на /sorry/, капча)."""


class AnswerUnavailableError(Exception):
    """Ответ не появился; screenshot содержит viewport для MCP-клиента."""

    code = "answer_unavailable"

    def __init__(self, message: str, screenshot: str | None = None):
        super().__init__(message)
        self.screenshot = screenshot


class AnswerRefusedError(AnswerUnavailableError):
    """Google принял вопрос, но ответа не дал («контент не сгенерирован»).

    Это не блокировка: профиль цел, вкладка жива, /sorry/ не было. Наблюдается
    на уточняющих вопросах с датацентрового адреса; тот же код на мобильном
    прокси второй ход отрабатывает.
    """

    code = "answer_refused"


class ProfilePoolExhaustedError(Exception):
    """Все сохранённые профили требуют ручной проверки."""


@dataclass
class SearchSession:
    page: object
    user_session_id: str
    created_at: datetime
    transcript: list = None  # [(вопрос, ответ), ...] — для контекста follow-up
    sources: list = None     # источники последнего ответа: [{index, url, title}, ...]

    def __post_init__(self):
        if self.transcript is None:
            self.transcript = []
        if self.sources is None:
            self.sources = []


# Капча без редиректа на /sorry/: разметка страницы проверки. Проверяется
# только там, где шаг уже провалился, — иначе это лишний round-trip на каждую
# навигацию. Разница осмысленная: с этой проверкой профиль уходит в ротацию,
# без неё тот же случай возвращался клиенту как невнятный таймаут.
CAPTCHA_DOM = 'form#captcha-form, iframe[src*="/recaptcha/"], div.g-recaptcha'


def _check_blocked(page, deep: bool = False):
    if "/sorry/" in page.url:
        raise BlockedError(f"заблокировано Google: {page.url}")
    if not deep:
        return
    try:
        found = page.locator(CAPTCHA_DOM).count() > 0
    except Exception:
        return  # страница уже недоступна — пусть падает исходная ошибка
    if found:
        raise BlockedError(f"капча на странице: {page.url}")


def _load_cookie_file():
    """Куки из GOOGLE_COOKIES_FILE — JSON-массив (Cookie-Editor) или Netscape cookies.txt.

    Тёплые куки живого браузера (NID/SOCS/AEC/DV и пр.) резко снижают частоту
    reCAPTCHA у холодного профиля. Формат приводим к виду Playwright add_cookies.
    """
    path = os.getenv("GOOGLE_COOKIES_FILE")
    if not path or not os.path.exists(path):
        return []
    text = open(path, encoding="utf-8", errors="replace").read().strip()
    raw = []
    if text.startswith("["):
        for c in json.loads(text):
            exp = c.get("expirationDate") or c.get("expires")
            raw.append((c.get("name"), c.get("value") or "", c.get("domain") or ".google.com",
                        c.get("path") or "/", bool(c.get("secure")),
                        int(exp) if isinstance(exp, (int, float)) and exp > 0 else 0))
    else:
        for line in text.splitlines():
            f = line.rstrip("\n").split("\t")
            if len(f) != 7 or line.startswith("#"):
                continue
            domain, _flag, cpath, secure, expiry, name, value = f
            raw.append((name, value, domain, cpath or "/", secure.upper() == "TRUE",
                        int(expiry) if expiry.isdigit() and int(expiry) > 0 else 0))
    return [_norm_cookie(*r) for r in raw if r[0]]


def _norm_cookie(name, value, domain, path, secure, expires):
    """Приводит к тому, что примет Playwright add_cookies (иначе он рубит всю пачку)."""
    if expires > 1e11:      # Firefox отдаёт expiry в миллисекундах
        expires //= 1000
    if expires:
        expires = min(expires, 4102444800)  # не дальше 2100 — Playwright капризен
    cookie = {"name": name, "value": value}
    if name.startswith("__Host-"):
        # host-only: только url (без domain/path), secure обязателен
        cookie["url"] = f"https://{domain.lstrip('.')}/"
        cookie["secure"] = True
    else:
        cookie["domain"] = domain
        cookie["path"] = path
        cookie["secure"] = secure or name.startswith("__Secure-")
    if expires:
        cookie["expires"] = expires
    return cookie


def _next_proxy():
    """Прокси из env PROXY_URL (http://user:pass@host:port) в формате Camoufox/Playwright.

    Один sticky-эндпоинт (напр. Astroproxy с ротацией внешнего IP по таймеру).
    Пула нет — все личности идут через него; смена IP на стороне провайдера.
    Не задан — None (прямое подключение).
    """
    raw = os.getenv("PROXY_URL")
    if not raw:
        return None
    parts = urlsplit(raw)
    proxy = {"server": f"{parts.scheme}://{parts.hostname}:{parts.port}"}
    if parts.username:
        proxy["username"] = unquote(parts.username)
        proxy["password"] = unquote(parts.password or "")
    return proxy


def _read_camou_config(options: dict) -> dict:
    """CAMOU_CONFIG из launch_options: он нарезан по env CAMOU_CONFIG_<n>."""
    env = options.get("env") or {}
    chunks = [
        (int(name.rsplit("_", 1)[1]), value)
        for name, value in env.items()
        if name.startswith("CAMOU_CONFIG_")
    ]
    if not chunks:
        return {}
    return json.loads("".join(value for _, value in sorted(chunks)))


def _clamp_window_position(device: dict) -> dict:
    """Загоняет окно внутрь экрана. Camoufox этого не делает.

    clamp_window_dimensions правит только ширину и высоту, а позицию
    (window.screenX/screenY) отдаёт как есть из BrowserForge — там она
    моделирует второй монитор и легко уезжает за пределы одиночного экрана
    (наблюдали screenX=3432 при screen.width=1680). Пока позиция
    перегенерировалась на каждом запуске, это была редкая неудача; в
    закреплённом устройстве она бы осталась навсегда.
    """
    for axis, size, avail in (("X", "width", "availWidth"), ("Y", "height", "availHeight")):
        limit = device.get(f"screen.{avail}") or device.get(f"screen.{size}")
        window = device.get(f"window.outer{'Width' if axis == 'X' else 'Height'}")
        position = device.get(f"window.screen{axis}")
        if limit is None or window is None or position is None:
            continue
        device[f"window.screen{axis}"] = min(max(position, 0), max(0, limit - window))
    return device


def _generate_device() -> dict:
    """Разовая генерация железа профиля: только постоянные свойства машины.

    geoip выключен намеренно — география в устройство не попадает, её на каждом
    запуске считает Camoufox по текущему exit-IP.
    """
    base = {key: value for key, value in _BASE_CONFIG.items() if key != "persistent_context"}
    options = camoufox_launch_options(**{**base, "geoip": False})
    config = _read_camou_config(options)
    return _clamp_window_position({
        key: value
        for key, value in config.items()
        if not key.startswith(VOLATILE_CONFIG_KEYS)
    })


class Browser:
    def __init__(self, worker_id: str = "worker"):
        self._worker_id = worker_id
        self._cm = None       # Camoufox context-manager, для ручного закрытия в shutdown()
        self._context = None  # запущенный BrowserContext
        self._sessions: dict[str, SearchSession] = {}
        root = os.getenv("AIFARM_PROFILE_ROOT", "").strip()
        direct_path = os.getenv("AIFARM_PROFILE_PATH", "").strip()
        count = int(os.getenv("AIFARM_PROFILE_COUNT", "10"))
        if count < 1:
            raise ValueError("AIFARM_PROFILE_COUNT must be positive")
        self._profile_pool = (
            [Path(root) / f"user{number}" for number in range(1, count + 1)]
            if root and not direct_path
            else []
        )
        self._profile_cursor = -1
        self._profile_budget = 0
        self._new_identity()

    def _new_identity(self):
        """Генерирует новый профиль/прокси. Сам браузер (пере)запустится лениво."""
        profile_path = os.getenv("AIFARM_PROFILE_PATH", "").strip()
        requested = os.getenv("AIFARM_PROFILE_ID", "").strip()
        if profile_path:
            self._profile_dir = profile_path
            self._reset_budget()
        elif self._profile_pool:
            self._select_next_profile()  # норму выставит _take_profile
        else:
            identity = (
                re.sub(r"[^A-Za-z0-9_-]", "_", requested)[:64]
                if requested
                else uuid.uuid4().hex[:8]
            )
            self._profile_dir = f"{PROFILES_DIR}/{self._worker_id}_{identity}"
            self._reset_budget()
        self._proxy = _next_proxy()

    @property
    def profile_id(self) -> str | None:
        return Path(self._profile_dir).name if self._profile_dir else None

    @staticmethod
    def _blocked_at(profile: Path) -> datetime | None:
        """Время блокировки из маркера. None — маркера нет либо он нечитаем."""
        try:
            return datetime.fromisoformat((profile / BLOCK_MARKER).read_text().strip())
        except (OSError, ValueError):
            return None

    def _take_profile(self, index: int) -> str:
        profile = self._profile_pool[index]
        profile.mkdir(parents=True, exist_ok=True)
        (profile / BLOCK_MARKER).unlink(missing_ok=True)  # вернулся в ротацию
        self._profile_cursor = index
        self._profile_dir = str(profile)
        self._reset_budget()
        return profile.name

    def _reset_budget(self):
        """Норма вопросов для только что взятого профиля, с разбросом."""
        self._profile_budget = random.randint(
            max(1, PROFILE_MAX_QUERIES - 2), max(1, PROFILE_MAX_QUERIES)
        )

    def _select_next_profile(self) -> str | None:
        """Следующий профиль по кругу: сначала остывшие, потом самый отдохнувший."""
        cooling: list[tuple[datetime, int]] = []
        now = datetime.now(UTC)
        for offset in range(1, len(self._profile_pool) + 1):
            index = (self._profile_cursor + offset) % len(self._profile_pool)
            profile = self._profile_pool[index]
            if not (profile / BLOCK_MARKER).exists():
                return self._take_profile(index)
            blocked_at = self._blocked_at(profile)
            if blocked_at is None:
                continue  # маркер поставлен руками — профиль ждёт ручного прогрева
            if now - blocked_at >= PROFILE_COOLDOWN:
                return self._take_profile(index)
            cooling.append((blocked_at, index))
        if cooling:  # все под блокировкой — берём тот, что отдыхал дольше всех
            blocked_at, index = min(cooling)
            print(f"все профили под блокировкой, беру {self._profile_pool[index].name} "
                  f"(заблокирован {blocked_at.isoformat()})", flush=True)
            return self._take_profile(index)
        self._profile_dir = None
        return None

    def _launch_config(self):
        # config= переживает генерацию фингерпринта: merge_into внутри Camoufox
        # ставит только отсутствующие ключи, поэтому сохранённое железо побеждает.
        return {
            **_BASE_CONFIG,
            "locale": BROWSER_LOCALE,
            "user_data_dir": self._profile_dir,
            "proxy": self._proxy,
            "config": self._device_config(),
        }

    def _device_config(self) -> dict:
        """Железо профиля: при первом обращении создаётся, дальше только читается."""
        path = Path(self._profile_dir) / DEVICE_FILE
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = None
        if saved is not None:
            repaired = _clamp_window_position(dict(saved))
            if repaired != saved:  # устройство записано до появления проверки
                path.write_text(json.dumps(repaired, indent=1, sort_keys=True), encoding="utf-8")
                print(f"профиль {self.profile_id}: окно возвращено в экран "
                      f"({saved.get('window.screenX')},{saved.get('window.screenY')} -> "
                      f"{repaired.get('window.screenX')},{repaired.get('window.screenY')})", flush=True)
            return repaired
        device = _generate_device()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(device, indent=1, sort_keys=True), encoding="utf-8")
        print(f"профиль {self.profile_id}: заведено устройство "
              f"{device.get('navigator.platform')} "
              f"{device.get('screen.width')}x{device.get('screen.height')}", flush=True)
        return device

    def _rotate_by_load(self):
        """Отработавший норму профиль уступает место следующему.

        Проверяется только перед НОВЫМ поиском: follow-up обязан остаться на той
        вкладке, где начался разговор, иначе продолжать нечего.
        """
        if PROFILE_MAX_QUERIES < 1 or not self._profile_pool or self._profile_budget > 0:
            return
        spent = self.profile_id
        self.shutdown()
        print(f"профиль {spent} отработал норму, перехожу на "
              f"{self._select_next_profile()}", flush=True)

    def _ensure_browser(self):
        if self._profile_dir is None:
            raise ProfilePoolExhaustedError("нет готовых профилей")
        if self._context is None:
            self._cm = Camoufox(**self._launch_config())
            self._context = self._cm.start()
            # Потолок на КАЖДЫЙ вызов Playwright. Без него у части вызовов
            # таймаута нет вовсе, и зависший браузер держит воркер бесконечно
            # (наблюдали 36 минут и 17 минут). Явные timeout= в коде сильнее.
            self._context.set_default_timeout(EL_TIMEOUT)
            self._context.set_default_navigation_timeout(NAV_TIMEOUT)
            if FLAG_SEED_COOKIES:
                self._seed_consent()
        return self._context

    def _seed_consent(self):
        """Cookie согласия + тёплые куки из файла (если задан) — до первой навигации."""
        cookies = [
            {"name": "SOCS", "value": "CAISNQgQEitib3FfaWRlbnRpdHlmcm9udGVuZF8yMDI0MDEwOS4wMV9wMBoCZW4gACIA",
             "domain": ".google.com", "path": "/"},
            {"name": "CONSENT", "value": "YES+cb", "domain": ".google.com", "path": "/"},
        ]
        file_cookies = _load_cookie_file()
        names = {c["name"] for c in file_cookies}
        cookies = [c for c in cookies if c["name"] not in names] + file_cookies
        ok = 0
        for ck in cookies:  # по одной — кривая кука не должна рубить всю пачку
            try:
                self._context.add_cookies([ck])
                ok += 1
            except Exception as error:
                print(f"cookie {ck.get('name')} rejected: {error}", flush=True)
        print(f"seeded {ok}/{len(cookies)} cookies", flush=True)

    _CONSENT_BTN = (
        "#L2AGLb, "
        "button[aria-label*='Accept all' i], button[aria-label*='agree' i], "
        "button[aria-label*='Zaakceptuj' i], button[aria-label*='akzeptieren' i], "
        "button:has-text('Accept all'), button:has-text('I agree'), "
        "button:has-text('Alle akzeptieren'), button:has-text('Ich stimme zu'), "
        "button:has-text('Zaakceptuj wszystko'), button:has-text('Tout accepter'), "
        "button:has-text('Aceptar todo'), button:has-text('Accetta tutto')"
    )

    def _accept_cookies(self, page):
        # Диалог "Zanim przejdziesz do Google" / "Before you continue" рендерится
        # и инлайн, и в iframe (consent.google.com), и на разных локалях. Ищем
        # кнопку "принять всё" во всех фреймах; #L2AGLb — id, от локали не зависит.
        if not FLAG_ACCEPT_CONSENT:
            return
        for attempt in range(4):
            try:  # поле поиска доступно и не перекрыто — диалога нет
                # Замер: ПЕРВАЯ проверка actionability после навигации стоит
                # ~1.9 с (дальше 0.04 с), а прежний потолок был 2500 мс — запаса
                # 600 мс. Не уложившись, код решал, что поле перекрыто диалогом,
                # и уходил искать несуществующую кнопку согласия по всем фреймам.
                page.locator(SEARCH_BOX).first.click(trial=True, timeout=CONSENT_PROBE_TIMEOUT)
                return
            except Exception:
                pass
            clicked = False
            for frame in page.frames:
                try:
                    btn = frame.locator(self._CONSENT_BTN).first
                    btn.wait_for(state="visible", timeout=2500)
                    btn.click(timeout=5000)
                    clicked = True
                    break
                except Exception:
                    continue
            if clicked:
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
                except Exception:
                    pass
            elif attempt:
                self._dump_page(page, "consent")
                return  # кнопку не нашли — дальше по коду упрёмся и залогируем
            page.wait_for_timeout(1500)

    _PUNCTUATION = ".,!?;:—…)»"

    def _key_delay(self, typed: str) -> float:
        """Пауза после набранного символа, мс."""
        delay = random.lognormvariate(math.log(KEY_DELAY_MEDIAN_MS), KEY_DELAY_SIGMA)
        delay = min(max(delay, KEY_DELAY_MIN_MS), KEY_DELAY_MAX_MS)
        if typed in self._PUNCTUATION:
            delay += random.uniform(80, 260)   # закончил мысль
        elif typed == " ":
            delay += random.uniform(40, 120)   # граница слова
        if random.random() < THINK_PAUSE_CHANCE:
            delay += random.uniform(300, 900)  # действительно задумался
        return delay

    def _focus_field(self, page, locator):
        """Ставит курсор в поле ввода. Клик — как человек, но не любой ценой.

        Строка поиска google.com периодически не проходит проверку playwright
        «stable»: та ждёт одинакового bounding box на двух подряд кадрах, а поле
        в этот момент анимируется. Замер: первая проверка после навигации стоит
        ~1.9 с, дальше 0.04 с, но в этом состоянии она не сходится вовсе — клик
        выстаивал весь таймаут и ронял всю сессию ("AI Mode не открылся"), хотя
        страница живая и поле на месте (rAF 60 fps, elementFromPoint попадает в
        саму textarea). Причина в самой анимации Google, снаружи она не чинится.

        Поле на google.com и так autofocus — человек, открывший страницу, просто
        печатает. Поэтому промах клика не повод валить поиск: ставим фокус.
        """
        try:
            locator.click(timeout=FIELD_CLICK_TIMEOUT)
        except Exception:
            print("поле не далось клику (playwright: not stable), ставлю фокус напрямую",
                  flush=True)
            locator.focus()
        page.wait_for_timeout(random.uniform(150, 400))  # прицелиться перед вводом

    def _human_type(self, page, locator, text, focus=True):
        if focus:
            self._focus_field(page, locator)
        for ch in text:
            page.keyboard.type(ch)
            page.wait_for_timeout(self._key_delay(ch))

    def _type_query(self, page, locator, text, focus=True):
        """Длинный запрос: тело вставляем, вручную допечатываем только хвост."""
        head, tail = text[:-HUMAN_TAIL_CHARS], text[-HUMAN_TAIL_CHARS:]
        if not head:
            self._human_type(page, locator, text, focus=focus)
            return
        if focus:
            self._focus_field(page, locator)
        locator.fill(head)
        page.wait_for_timeout(random.uniform(200, 500))  # перечитать вставленное
        page.keyboard.press("End")
        self._human_type(page, locator, tail, focus=False)

    def _point_at_answer(self, page) -> bool:
        """Ставит курсор внутрь блока ответа перед прокруткой.

        page.mouse.wheel() крутит колесо ТАМ, ГДЕ СЕЙЧАС КУРСОР, а курсор после
        старта стоит в углу (0,0). У AI Mode контент лежит в своём скроллящемся
        контейнере, поэтому колесо из угла листало что угодно, только не ответ:
        со стороны это «сгенерировал простыню и не сдвинул её ни на пиксель»,
        то есть ровно тот признак, ради борьбы с которым прокрутка и писалась.
        """
        try:
            box = page.locator(AI_ANSWER).last.bounding_box()
        except Exception:
            box = None
        if not box:
            return False
        page.mouse.move(
            box["x"] + box["width"] * random.uniform(0.35, 0.65),
            box["y"] + box["height"] * random.uniform(0.25, 0.6),
            steps=random.randint(6, 14),
        )
        page.wait_for_timeout(random.uniform(80, 260))
        return True

    def _scroll_gesture(self, page, distance):
        """Один жест прокрутки: мелкие щелчки по синусоиде — разгон и торможение.

        Вес каждого щелчка sin(pi * фаза): по краям жеста мелкие, в середине
        крупные. Сумма нормируется на distance, поэтому куда доедем — известно,
        а вот КАК доедем, выглядит как живая рука, а не как один телепорт.
        """
        ticks = max(6, int(distance / random.uniform(*SCROLL_TICK_PX)))
        weights = [math.sin(math.pi * (i + 0.5) / ticks) for i in range(ticks)]
        scale = distance / sum(weights)
        for weight in weights:
            page.mouse.wheel(0, weight * scale * random.uniform(0.85, 1.15))
            page.wait_for_timeout(random.uniform(*SCROLL_TICK_MS))

    def _read_scroll(self, page, steps=1):
        """Прокрутка ответа в темпе чтения. Косметика: сбой не должен ронять поиск."""
        if not FLAG_READ_SCROLL:
            return
        self._point_at_answer(page)  # без этого колесо крутится мимо ответа
        for _ in range(steps):
            try:
                self._scroll_gesture(page, random.uniform(*SCROLL_STEP_PX))
            except Exception as error:
                print(f"прокрутка не прошла: {error.__class__.__name__}", flush=True)
                return
            page.wait_for_timeout(random.uniform(*SCROLL_PAUSE_MS))

    def _read_pause_seconds(self, answer: str) -> float:
        """Сколько «читать» предыдущий ответ перед уточняющим вопросом."""
        per_100 = random.uniform(*FOLLOWUP_READ_S_PER_100)
        seconds = len(answer) / 100 * per_100
        return min(max(seconds, FOLLOWUP_READ_MIN_S), FOLLOWUP_READ_MAX_S)

    def _read_answer(self, page, answer: str) -> None:
        """Проводит паузу чтения, листая ответ, а не замерев на месте."""
        deadline = time.monotonic() + self._read_pause_seconds(answer)
        while time.monotonic() < deadline:
            self._read_scroll(page)
            page.wait_for_timeout(random.uniform(600, 1600))

    def _take_free_page(self, context):
        # Camoufox стартует с одной пустой about:blank вкладкой. Если её
        # не переиспользовать, она копится рядом с вкладками сессий.
        for p in context.pages:
            if p.url in ("about:blank", ""):
                return p
        return context.new_page()

    def _take_search_page(self):
        """Берёт вкладку, один раз перезапуская аварийно закрытый Camoufox."""
        try:
            return self._take_free_page(self._ensure_browser())
        except TargetClosedError:
            print("браузер закрылся, перезапускаю", flush=True)
            self.shutdown()
            return self._take_free_page(self._ensure_browser())

    def _open_ai_mode(self, page, prompt: str, human_tail: str = None):
        """google.com → ввод запроса → клик по вкладке "Режим ИИ" → ждём ответ.

        human_tail: если задан — prompt вставляется целиком (fill), а human_tail
        допечатывается по-человечески в конце (для длинного контекста + короткого
        живого вопроса). Если не задан — хвост отрезается автоматически, см.
        _type_query: посимвольно набирать весь длинный промпт нельзя.
        """
        page.goto(GOOGLE_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        self._accept_cookies(page)

        box = page.locator(SEARCH_BOX)
        if human_tail is None:
            self._type_query(page, box, prompt)
        else:
            box.click()
            box.fill(prompt)
            page.wait_for_timeout(random.uniform(200, 500))
            page.keyboard.press("End")
            self._human_type(page, box, human_tail, focus=False)
        page.wait_for_timeout(random.uniform(400, 1100))  # перечитать перед Enter
        box.press("Enter")
        page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
        _check_blocked(page)

        # Обычно переходим кликом по вкладке. Google иногда убирает её из шапки
        # обычной выдачи; тогда открываем ту же выдачу с udm=50 напрямую.
        link = page.locator(AI_MODE_LINK).first
        try:
            link.wait_for(state="visible", timeout=EL_TIMEOUT)
        except Exception:
            page.goto(_ai_mode_url(page.url), wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        else:
            page.wait_for_timeout(random.uniform(300, 900))
            link.click()
        page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
        _check_blocked(page)
        try:
            page.locator(AI_ANSWER).last.wait_for(state="visible", timeout=EL_TIMEOUT)
        except Exception as error:
            self._dump_page(page, "answer_missing")
            _check_blocked(page, deep=True)
            raise AnswerUnavailableError(
                "контейнер ответа AI Mode не появился", self._capture_screenshot(page)
            ) from error

    def new_search_session(self, user_session_id: str, query: str) -> str:
        """Открывает вкладку, ищет query, переходит в AI Mode. Возвращает session_id."""
        self._expire_sessions()
        self._rotate_by_load()
        self._profile_budget -= 1
        page = self._take_search_page()
        page.bring_to_front()
        try:
            self._open_ai_mode(page, query)
        except AnswerUnavailableError:
            raise
        except Exception as error:
            self._dump_page(page, "fail")
            # /sorry/ мог прилететь редиректом уже после проверки, а капчу
            # Google иногда показывает прямо на выдаче, без редиректа.
            _check_blocked(page, deep=True)
            raise AnswerUnavailableError(
                "AI Mode не открылся", self._capture_screenshot(page)
            ) from error

        session_id = uuid.uuid4().hex[:8]
        session = SearchSession(page, user_session_id, datetime.now(UTC))
        session.transcript.append([query, None])  # ответ допишет get_last_ai_answer
        self._sessions[session_id] = session
        return self._public_session_id(session_id)

    def ask_followup(self, user_session_id: str, session_id: str, query: str) -> None:
        """Задаёт новый вопрос в том же диалоге AI Mode, той же вкладкой."""
        session = self._session(user_session_id, session_id)
        self._profile_budget -= 1
        page = session.page
        page.bring_to_front()

        previous_answers = page.locator(AI_ANSWER).count()
        previous_text = page.locator(AI_ANSWER).last.inner_text().strip() if previous_answers else ""
        # Сначала дочитываем предыдущий ответ и только потом тянемся к полю:
        # без этой паузы следующий вопрос начинал печататься через пару секунд
        # после того, как Google дописал простыню.
        self._read_answer(page, previous_text)

        box = page.locator(FOLLOWUP_INPUT).first
        try:
            box.wait_for(state="visible", timeout=EL_TIMEOUT)
        except Exception as error:
            self._dump_page(page, "followup_input_missing")
            _check_blocked(page, deep=True)
            raise AnswerUnavailableError(
                "поле уточняющего вопроса AI Mode не найдено",
                self._capture_screenshot(page),
            ) from error
        sent_query = _followup_key_layout(query)
        self._type_query(page, box, sent_query)
        page.wait_for_timeout(random.uniform(600, 1800))  # перечитать набранное перед отправкой
        box.press("Enter")

        # Ждём ЛИБО ответ, ЛИБО отказ. Ответ приходит двумя способами: новый
        # main-col или дописанный текст в существующем. Отказ Google рисует
        # своим блоком ANSWER_ERROR («контент не сгенерирован») — без этой
        # ветки мы просто выстаивали все ANSWER_START_TIMEOUT и возвращали
        # невнятный таймаут вместо понятной причины.
        try:
            page.wait_for_function(
                """([selector, count, text, errorSelector]) => {
                    if (document.querySelector(errorSelector)) return true;
                    const blocks = document.querySelectorAll(selector);
                    if (blocks.length > count) return true;
                    const last = blocks[blocks.length - 1];
                    return !!last && last.innerText.trim() !== text;
                }""",
                arg=[AI_ANSWER, previous_answers, previous_text, ANSWER_ERROR],
                timeout=ANSWER_START_TIMEOUT,
            )
        except Exception:
            # Скриншот + HTML именно в момент, когда ответа не дождались:
            # понять, ушёл ли вопрос вообще и что показывает страница, иначе
            # можно только гадать по таймауту.
            self._dump_page(page, "followup_fail")
            _check_blocked(page, deep=True)
            raise AnswerUnavailableError(
                "ответ на уточняющий вопрос не появился", self._capture_screenshot(page)
            ) from None

        refusals = page.locator(ANSWER_ERROR)
        if refusals.count() > 0:
            self._dump_page(page, "followup_refused")
            raise AnswerRefusedError(
                refusals.last.inner_text().strip()[:200], self._capture_screenshot(page)
            )

        # get_last_ai_answer() fills the answer after the worker receives it.
        session.transcript.append([sent_query, None])

    def get_last_ai_answer(self, user_session_id: str, session_id: str) -> str:
        """Текст ответа AI Mode на последний вопрос этой сессии.

        Каждый вопрос (первый и follow-up) — свежий поиск AI Mode с одним ответом
        в main-col, поэтому берём его целиком.
        """
        session = self._session(user_session_id, session_id)
        page = session.page
        answer = page.locator(AI_ANSWER).last
        answer.wait_for(state="visible", timeout=EL_TIMEOUT)

        # AI Mode streams text into an already-visible container.  Visibility
        # alone therefore races the stream and can return only its heading.
        deadline = time.monotonic() + STREAM_TIMEOUT
        previous = ""
        stable_since = time.monotonic()
        next_scroll = time.monotonic() + random.uniform(1.5, 3.0)
        while time.monotonic() < deadline:
            current = answer.inner_text().strip()
            now = time.monotonic()
            if current != previous:
                previous = current
                stable_since = now
            elif current and now - stable_since >= 1.5:
                if os.getenv("AIFARM_DUMP_ANSWER"):
                    self._dump_page(page, "ok")
                text, sources = self._answer_parts(answer, current)
                # Если ответ дописался в тот же блок, в тексте лежит и прошлый
                # ход диалога — отдавать его повторно нельзя.
                if len(session.transcript) >= 2:
                    earlier = session.transcript[-2][1]
                    if earlier and text.startswith(earlier):
                        text = text[len(earlier):].lstrip()
                if session.transcript and session.transcript[-1][1] is None:
                    session.transcript[-1][1] = text  # для контекста будущих follow-up
                # Нумерация маркеров сквозная по блоку, поэтому список источников
                # прошлого хода остаётся: номера в тексте должны сходиться.
                session.sources = sources
                self._read_scroll(page, steps=random.randint(2, 4))  # дочитать до конца
                return text
            if now >= next_scroll:  # читаем по мере того, как текст пишется
                self._read_scroll(page)
                next_scroll = time.monotonic() + random.uniform(1.2, 2.5)
            else:
                page.wait_for_timeout(250)
        raise AnswerUnavailableError(
            "ответ AI Mode не завершил генерацию", self._capture_screenshot(page)
        )

    def _answer_parts(self, answer, raw: str) -> tuple[str, list[dict]]:
        """Текст ответа (с маркерами [n]) и источники под этими маркерами.

        Разбор целиком в браузере (_ANSWER_JS) — один вызов на ответ. Если он
        не удался или разметка Google поехала, остаётся сырой текст без
        источников: поиск от этого не падает. Пустоту не возвращаем никогда.
        """
        try:
            parsed = answer.evaluate(_ANSWER_JS, {"denoise": FLAG_DENOISE})
        except Exception:
            parsed = None
        if not isinstance(parsed, dict):
            parsed = {}
        cleaned = _clean_text(parsed.get("text") or "") or _clean_text(raw)
        if os.getenv("AIFARM_DUMP_ANSWER"):
            self._dump_answer(answer, raw, cleaned)
        return cleaned, _valid_sources(parsed.get("sources"))

    def last_sources(self, user_session_id: str, session_id: str) -> list[dict]:
        """Источники последнего ответа этой сессии (пусто, пока ответа нет)."""
        return list(self._session(user_session_id, session_id).sources)

    @staticmethod
    def _capture_screenshot(page) -> str | None:
        """Viewport PNG для возврата клиенту; диагностика не должна зависнуть."""
        try:
            png = page.screenshot(type="png", full_page=False, timeout=5000)
            return base64.b64encode(png).decode("ascii")
        except Exception:
            return None

    def _dump_page(self, page, tag: str):
        """Скриншот + HTML + URL страницы при сбое — включается AIFARM_DUMP_ANSWER."""
        base = os.getenv("AIFARM_DUMP_ANSWER")
        if not base:
            return
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        try:
            os.makedirs(base, exist_ok=True)
            page.screenshot(path=f"{base}/{self._worker_id}_{stamp}_{tag}.png", full_page=True)
            for suffix, data in (("html", page.content()), ("url", page.url)):
                with open(f"{base}/{self._worker_id}_{stamp}_{tag}.{suffix}", "w") as fh:
                    fh.write(data)
        except Exception:
            pass

    def _dump_answer(self, answer, raw: str, cleaned: str):
        """Диагностика денойза: сохраняет HTML/сырой/чистый текст ответа.

        Включается переменной AIFARM_DUMP_ANSWER=<каталог>. Нужна один раз,
        чтобы подобрать фильтр UI-строк по реальному DOM Google.
        """
        base = os.getenv("AIFARM_DUMP_ANSWER")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        try:
            html = answer.inner_html()
        except Exception:
            html = ""
        try:
            os.makedirs(base, exist_ok=True)
            for suffix, data in (("html", html), ("raw.txt", raw), ("clean.txt", cleaned)):
                with open(f"{base}/{self._worker_id}_{stamp}.{suffix}", "w") as fh:
                    fh.write(data)
        except Exception:
            pass

    def list_sessions(self, user_session_id: str) -> list[str]:
        self._expire_sessions()
        return [
            self._public_session_id(session_id)
            for session_id, session in self._sessions.items()
            if session.user_session_id == user_session_id
        ]

    def close_session(self, user_session_id: str, session_id: str):
        session = self._session(user_session_id, session_id)
        self._sessions.pop(self._local_session_id(session_id))
        session.page.close()

    def _session(self, user_session_id: str, session_id: str) -> SearchSession:
        self._expire_sessions()
        session = self._sessions.get(self._local_session_id(session_id))
        if session is None or session.user_session_id != user_session_id:
            raise KeyError(session_id)
        return session

    def _public_session_id(self, session_id: str) -> str:
        return f"{self._worker_id}:{session_id}"

    def _local_session_id(self, session_id: str) -> str:
        worker, separator, local = session_id.partition(":")
        if worker != self._worker_id or not separator or not local:
            raise KeyError(session_id)
        return local

    def _expire_sessions(self):
        deadline = datetime.now(UTC) - SESSION_TTL
        for session_id, session in list(self._sessions.items()):
            if session.created_at <= deadline:
                self._sessions.pop(session_id)
                try:
                    session.page.close()
                except Exception:
                    pass

    def shutdown(self):
        """Закрывает браузер целиком — все вкладки/сессии этого объекта."""
        if self._cm is not None:
            self._cm.__exit__(None, None, None)
        self._cm = None
        self._context = None
        self._sessions.clear()

    def burn_and_rotate(self):
        """Помечает профиль временем блокировки и переключается на следующий.

        Ничего не удаляется: профиль с куками остаётся на диске и вернётся в
        ротацию сам через PROFILE_COOLDOWN (см. _select_next_profile).
        """
        if self._profile_pool:
            blocked_profile = self.profile_id
            marker = Path(self._profile_dir) / BLOCK_MARKER
            marker.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
            self.shutdown()
            return blocked_profile, self._select_next_profile()
        old_profile_dir = self._profile_dir
        self.shutdown()
        self._new_identity()
        return Path(old_profile_dir).name, self.profile_id
