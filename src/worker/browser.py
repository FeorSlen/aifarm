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
from urllib.parse import quote_plus, unquote, urlsplit

from camoufox.sync_api import Camoufox

GOOGLE_URL = "https://www.google.com"
SEARCH_BOX = "textarea[name='q']"
ACCEPT_COOKIES_BTN = "#L2AGLb"  # id кнопки "Accept all" у Google, не зависит от локали
AI_MODE_PARAM = "udm=50"  # AI Mode; добавляем в URL вместо клика по вкладке "Режим ИИ"
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
)


class BlockedError(Exception):
    """Google принял браузер за бота (редирект на /sorry/, капча)."""


@dataclass
class SearchSession:
    page: object
    user_session_id: str
    created_at: datetime
    transcript: list = None  # [(вопрос, ответ), ...] — для контекста follow-up

    def __post_init__(self):
        if self.transcript is None:
            self.transcript = []


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

    def _human_type(self, page, locator, text, focus=True):
        if focus:
            locator.click()
            page.wait_for_timeout(random.uniform(150, 400))  # прицелиться перед вводом
        for ch in text:
            page.keyboard.type(ch)
            delay = random.uniform(70, 160)
            if ch == " " or random.random() < 0.06:  # пауза на пробеле / изредка «задумался»
                delay += random.uniform(120, 350)
            page.wait_for_timeout(delay)

    def _take_free_page(self, context):
        # Camoufox стартует с одной пустой about:blank вкладкой. Если её
        # не переиспользовать, она копится рядом с вкладками сессий.
        for p in context.pages:
            if p.url in ("about:blank", ""):
                return p
        return context.new_page()

    def _open_ai_mode(self, page, prompt: str, human_tail: str = None):
        """google.com → ввод prompt → AI Mode по udm=50 → ждём контейнер ответа.

        human_tail: если задан — prompt вставляется целиком (fill), а human_tail
        допечатывается по-человечески в конце (для длинного контекста + короткого
        живого вопроса).
        """
        page.goto(GOOGLE_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        self._accept_cookies(page)

        box = page.locator(SEARCH_BOX)
        if human_tail is None:
            self._human_type(page, box, prompt)
            typed = prompt
        else:
            box.click()
            box.fill(prompt)
            page.wait_for_timeout(random.uniform(200, 500))
            page.keyboard.press("End")
            self._human_type(page, box, human_tail, focus=False)
            typed = prompt + human_tail
        page.wait_for_timeout(random.uniform(400, 1100))  # перечитать перед Enter
        box.press("Enter")
        page.wait_for_load_state("load", timeout=NAV_TIMEOUT)  # нужен settled page.url
        _check_blocked(page)

        # В AI Mode — добавлением udm=50 к URL выдачи (ровно то, что делает клик по
        # вкладке "Режим ИИ"), без поиска и клика по самой ссылке.
        url = page.url.split("#")[0]
        if AI_MODE_PARAM in url:
            pass  # Enter уже привёл в AI Mode
        elif "/search" in url:
            page.goto(url + ("&" if "?" in url else "?") + AI_MODE_PARAM,
                      wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        else:  # Enter не долетел до /search — строим URL AI Mode из запроса
            page.goto(f"{GOOGLE_URL}/search?q={quote_plus(typed[:1500])}&{AI_MODE_PARAM}",
                      wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        _check_blocked(page)
        page.locator(AI_ANSWER).last.wait_for(state="visible", timeout=EL_TIMEOUT)

    def new_search_session(self, user_session_id: str, query: str) -> str:
        """Открывает вкладку, ищет query, переходит в AI Mode. Возвращает session_id."""
        self._expire_sessions()
        context = self._ensure_browser()
        page = self._take_free_page(context)
        page.bring_to_front()
        try:
            self._open_ai_mode(page, query)
        except Exception:
            self._dump_page(page, "fail")
            _check_blocked(page)  # /sorry/ мог прилететь редиректом уже после проверки
            raise

        session_id = uuid.uuid4().hex[:8]
        session = SearchSession(page, user_session_id, datetime.now(UTC))
        session.transcript.append([query, None])  # ответ допишет get_last_ai_answer
        self._sessions[session_id] = session
        return self._public_session_id(session_id)

    def ask_followup(self, user_session_id: str, session_id: str, query: str) -> None:
        """AI Mode не продолжает диалог без логина (а логин из VPS не встаёт).

        Поэтому follow-up = НОВЫЙ поиск AI Mode той же вкладкой, с подшитым
        контекстом предыдущих ходов. Контекст вставляется fill'ом, сам вопрос —
        по-человечески.
        """
        session = self._session(user_session_id, session_id)
        page = session.page
        page.bring_to_front()

        ctx = "Продолжается обсуждение. Ранее:\n"
        for q, a in session.transcript[-3:]:
            ctx += f"— вопрос: {q}\n"
            if a:
                ctx += f"  ответ: {a[:700]}\n"
        ctx = ctx[:6000] + "\n\nС учётом этого ответь на новый вопрос: "

        try:
            self._open_ai_mode(page, ctx, human_tail=query)
        except Exception:
            self._dump_page(page, "followup-fail")
            _check_blocked(page)
            raise
        session.transcript.append([query, None])

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
        while time.monotonic() < deadline:
            current = answer.inner_text().strip()
            now = time.monotonic()
            if current != previous:
                previous = current
                stable_since = now
            elif current and now - stable_since >= 1.5:
                if os.getenv("AIFARM_DUMP_ANSWER"):
                    self._dump_page(page, "ok")
                text = self._denoise(answer, current)
                if session.transcript and session.transcript[-1][1] is None:
                    session.transcript[-1][1] = text  # для контекста будущих follow-up
                return text
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
