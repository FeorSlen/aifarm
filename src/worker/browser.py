"""
Контракт с main:

    br = Browser(worker_id="worker1")
    session_id = br.new_search_session(user_session_id, query) -> str
    br.ask_followup(user_session_id, session_id, query)
    text = br.get_last_ai_answer(user_session_id, session_id)

Один Browser = один живой браузер/профиль/куки на всё время жизни текущей
"личности". Личность — это профиль на диске + прокси; когда её спалили
(BlockedError), br.burn_and_rotate() сносит профиль и заводит новую —
сам объект Browser переживает это, session_id старой личности после
ротации, естественно, недействительны.
"""

import json
import os
import random
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urlsplit

from camoufox.sync_api import Camoufox

GOOGLE_URL = "https://www.google.com"
SEARCH_BOX = "textarea[name='q']"
ACCEPT_COOKIES_BTN = "#L2AGLb"  # id кнопки "Accept all" у Google, не зависит от локали
AI_MODE_LINK = 'a[href*="udm=50"]'  # udm=50 = AI Mode, тоже не зависит от локали
FOLLOWUP_INPUT = 'textarea.ITIRGe, textarea[placeholder="Задайте вопрос"]'
AI_ANSWER = '[data-container-id="main-col"]'  # один такой блок на ход диалога AI Mode

PROFILES_DIR = os.getenv("PROFILES_DIR", "./profiles")
SESSION_TTL = timedelta(hours=24)

# Таймауты рассчитаны на медленный релей через прокси (residential/mobile).
# Все — потолки, срабатывают только при зависании; на живом соединении не мешают.
NAV_TIMEOUT = int(os.getenv("NAV_TIMEOUT_MS", "45000"))   # goto / load-state
EL_TIMEOUT = int(os.getenv("EL_TIMEOUT_MS", "20000"))     # ожидание элементов (AI Mode, поле, ответ)
STREAM_TIMEOUT = int(os.getenv("STREAM_TIMEOUT_S", "45"))  # стриминг ответа AI Mode


def _flag(name, default=True):
    return os.getenv(name, str(default)).strip().lower() not in ("0", "false", "no", "off", "")


# --- переключатели поведения (env, без ребилда; см. README) ---
FLAG_HUMANIZE = _flag("AIFARM_HUMANIZE")              # человекоподобные движения курсора Camoufox
FLAG_GEOIP = _flag("AIFARM_GEOIP")                    # подгонка TZ/локали под exit-IP прокси
FLAG_BLOCK_WEBRTC = _flag("AIFARM_BLOCK_WEBRTC")      # блок утечки реального IP через WebRTC
FLAG_ACCEPT_CONSENT = _flag("AIFARM_ACCEPT_CONSENT")  # авто-принятие диалога согласия Google
FLAG_DENOISE = _flag("AIFARM_DENOISE")                # чистка ответа AI Mode от UI-обвязки
FLAG_SEED_COOKIES = _flag("AIFARM_SEED_COOKIES")      # инъекция куков (файл + дефолтное согласие)
FLAG_BURN_ON_BLOCK = _flag("AIFARM_BURN_ON_BLOCK")    # сносить профиль/сессию при /sorry/

# Денойз ответа AI Mode: убираем с клона ноды UI-обвязку. Список консервативный
# и структурный (роли/теги, не классы Google — те гниют за недели). ВНИМАНИЕ:
# 'sup' под вопросом — Google может рендерить в нём степени/сноски; проверить на
# дампе (AIFARM_DUMP_ANSWER) и при необходимости сузить до 'sup:has(a)'.
_DENOISE_JS = """
(el) => {
  const clone = el.cloneNode(true);
  clone.querySelectorAll(
    'button, [role="button"], [role="menu"], [role="menuitem"], [role="dialog"],'
    + ' [role="listbox"], [role="tablist"], [role="navigation"], svg, script, style,'
    + ' [aria-hidden="true"], sup'
  ).forEach((n) => n.remove());
  clone.style.position = 'fixed';
  clone.style.left = '-99999px';
  clone.style.top = '0';
  document.body.appendChild(clone);
  try {
    return clone.innerText;
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

# Настройки, общие для любой личности (сам fingerprint-профиль/прокси —
# за экземпляром Browser, см. Browser._launch_config).
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


@dataclass
class SearchSession:
    page: object
    user_session_id: str
    created_at: datetime


def _check_blocked(page):
    if "/sorry/" in page.url:
        raise BlockedError(f"заблокировано Google: {page.url}")


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


class Browser:
    def __init__(self, worker_id: str = "worker"):
        self._worker_id = worker_id
        self._cm = None       # Camoufox context-manager, для ручного закрытия в shutdown()
        self._context = None  # запущенный BrowserContext
        self._sessions: dict[str, SearchSession] = {}
        self._new_identity()

    def _new_identity(self):
        """Генерирует новый профиль/прокси. Сам браузер (пере)запустится лениво."""
        identity = uuid.uuid4().hex[:8]
        self._profile_dir = f"{PROFILES_DIR}/{self._worker_id}_{identity}"
        self._proxy = _next_proxy()

    def _launch_config(self):
        return {**_BASE_CONFIG, "user_data_dir": self._profile_dir, "proxy": self._proxy}

    def _ensure_browser(self):
        if self._context is None:
            self._cm = Camoufox(**self._launch_config())
            self._context = self._cm.start()
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
                page.locator(SEARCH_BOX).first.click(trial=True, timeout=2500)
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

    def _human_type(self, page, locator, text):
        locator.click()
        for ch in text:
            page.keyboard.type(ch)
            page.wait_for_timeout(random.uniform(60, 180))

    def _take_free_page(self, context):
        # Camoufox стартует с одной пустой about:blank вкладкой. Если её
        # не переиспользовать, она копится рядом с вкладками сессий.
        for p in context.pages:
            if p.url in ("about:blank", ""):
                return p
        return context.new_page()

    def new_search_session(self, user_session_id: str, query: str) -> str:
        """Открывает вкладку, ищет query, переходит в AI Mode. Возвращает session_id."""
        self._expire_sessions()
        context = self._ensure_browser()
        page = self._take_free_page(context)
        page.bring_to_front()

        try:
            page.goto(GOOGLE_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            self._accept_cookies(page)

            search_box = page.locator(SEARCH_BOX)
            self._human_type(page, search_box, query)
            page.wait_for_timeout(random.uniform(200, 500))
            search_box.press("Enter")
            page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
            _check_blocked(page)

            ai_mode_link = page.locator(AI_MODE_LINK).first
            ai_mode_link.wait_for(state="visible", timeout=EL_TIMEOUT)
            ai_mode_link.click()
            page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
            page.locator(AI_ANSWER).last.wait_for(state="visible", timeout=EL_TIMEOUT)
        except Exception:
            self._dump_page(page, "fail")
            _check_blocked(page)  # /sorry/ мог прилететь редиректом уже после проверки
            raise

        session_id = uuid.uuid4().hex[:8]
        self._sessions[session_id] = SearchSession(page, user_session_id, datetime.now(UTC))
        return self._public_session_id(session_id)

    def ask_followup(self, user_session_id: str, session_id: str, query: str) -> None:
        """Задаёт новый вопрос в том же диалоге AI Mode, той же вкладкой."""
        page = self._session(user_session_id, session_id).page
        page.bring_to_front()  # фоновая вкладка не рисует кадры, actionability-чеки виснут

        prev_count = page.locator(AI_ANSWER).count()
        box = page.locator(FOLLOWUP_INPUT).first
        box.wait_for(state="visible", timeout=EL_TIMEOUT)
        self._human_type(page, box, query)
        page.wait_for_timeout(random.uniform(200, 500))
        box.press("Enter")

        try:
            page.wait_for_function(
                f"expected => document.querySelectorAll('{AI_ANSWER}').length > expected",
                arg=prev_count,
                timeout=EL_TIMEOUT,
            )
        except Exception:
            # follow-up идёт через AJAX, а не полную навигацию — URL может
            # не смениться на /sorry/ даже при блокировке, так что это не
            # железобетонная проверка, но лучше, чем голый TimeoutError
            _check_blocked(page)
            raise

    def get_last_ai_answer(self, user_session_id: str, session_id: str) -> str:
        """Текст последнего ответа в диалоге AI Mode этой сессии."""
        page = self._session(user_session_id, session_id).page
        answer = page.locator(AI_ANSWER).last
        answer.wait_for(state="visible", timeout=EL_TIMEOUT)

        # AI Mode streams text into an already-visible container.  Visibility
        # alone therefore races the stream and can return only its heading.
        deadline = time.monotonic() + STREAM_TIMEOUT
        previous = ""
        stable_since = time.monotonic()
        while time.monotonic() < deadline:
            current = answer.inner_text().strip()
            now = time.monotonic()
            if current != previous:
                previous = current
                stable_since = now
            elif current and now - stable_since >= 1.5:
                return self._denoise(answer, current)
            page.wait_for_timeout(250)
        raise TimeoutError("AI answer did not finish streaming")

    def _denoise(self, answer, raw: str) -> str:
        """Чистит ответ AI Mode от UI-обвязки (кнопки-цитаты, иконки, сноски).

        Стрип структурный: на клоне ноды (живой DOM трогать нельзя — на нём
        висит счётчик ходов ask_followup и JS Google) выкидываются заведомо
        не-текстовые элементы, затем читается innerText. Если стрип срезал
        всё — отдаём сырой текст, пустоту не возвращаем никогда.
        """
        if not FLAG_DENOISE:
            return _clean_text(raw)
        try:
            stripped = answer.evaluate(_DENOISE_JS)
        except Exception:
            stripped = raw
        cleaned = _clean_text(stripped) or _clean_text(raw)
        if os.getenv("AIFARM_DUMP_ANSWER"):
            self._dump_answer(answer, raw, cleaned)
        return cleaned

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
        """
        Личность спалена: закрыть браузер, снести профиль с диска, взять
        новый профиль/прокси. Все текущие session_id умирают вместе со
        старыми cookies — это ожидаемо, они были частью сожжённой личности.

        AIFARM_BURN_ON_BLOCK=false — не делаем ничего: профиль/сессии/куки
        остаются как есть (для отладки и прогрева на живых куках).
        """
        if not FLAG_BURN_ON_BLOCK:
            return
        old_profile_dir = self._profile_dir
        self.shutdown()
        shutil.rmtree(old_profile_dir, ignore_errors=True)
        self._new_identity()
