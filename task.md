# Задачи aifarm

## Старое

1. Сделать денойзинг и требовать поисковый запрос от пользователя
2. Сделать возможность нескольких воркеров

## Из сравнения с тремя Google Search MCP (12.09.2026)

Полный разбор и обоснование: [research/google-mcp-comparison-2026-09-12.md](research/google-mcp-comparison-2026-09-12.md).
Ссылки ниже прибиты к конкретным коммитам, чтобы не поехали: `google-ai-mode-mcp` @ `04eb0ba`,
`google-surf-mcp` @ `b331d06`, `google-search-mcp` @ `94afd22`.

---

### P0. Структурированные цитаты AI Mode

**Проблема.** `search` возвращает голый текст. `_DENOISE_JS` (`src/worker/browser.py:175-181`) намеренно
срезает `sup` и `[aria-hidden="true"]`, то есть ровно те узлы, которые несут сноски. Клиент получает ответ
без единого источника — это единственная фича, где чужой проект объективно впереди.

**Откуда тянуть логику.**

- [`response-parser.ts` — разметка кнопок цитат, клик, сбор ссылок из `rhs-col`](https://github.com/PleasePrompto/google-ai-mode-mcp/blob/04eb0bad75f7bde830dcd082e89fef747b9ab7de/src/search/response-parser.ts#L90-L190)
  — селекторы кнопок перебираются списком (мультиязычность), после клика источники читаются из
  `[data-container-id="rhs-col"]`, маркер в тексте заменяется на `[CITE-n]`.
- [`markdown-converter.ts` — `embedCitations`, `[CITE-n]` → `[1][2]` + дедуплицированный список](https://github.com/PleasePrompto/google-ai-mode-mcp/blob/04eb0bad75f7bde830dcd082e89fef747b9ab7de/src/search/markdown-converter.ts#L73-L130)
  — цитаты сортируются по `marker_id` в обратном порядке, чтобы замена не сбивала индексы.

**Чего из их кода НЕ повторять.** Весь клик-цикл крутится внутри одного `page.evaluate` с синхронным
busy-wait (`while (Date.now() - start < ms) {}`, `response-parser.ts:78-79,143-163`). Это занимает main
thread страницы, из-за чего sidebar физически не успевает перерисоваться — их же таймауты лечат симптом.

**Как делать у нас.**

- Клики — обычными Playwright-операциями по одной кнопке, вне `page.evaluate`.
- После клика ждать mutation либо смену нормализованного набора ссылок в `rhs-col`, не `sleep`.
- Хранить `citation -> sources` плюс глобальный дедуплицированный `sources`.
- Отдавать `structured_content = {tab_id, answer, citations, sources}`; расширить `Result`
  (`src/contract.py:13-21`) новым необязательным полем, версию контракта не ломать.
- Сузить denoise: `sup:has(a)` оставлять, а не выкидывать (там уже висит TODO,
  `src/worker/browser.py:173-174`).
- При дрейфе DOM — тихий fallback на нынешний plain text, поиск не валить.

**Критерий готовности.** На выборке 50+ ответов ручная сверка даёт ≥95% верных привязок сноска→источник,
задержка приемлема, `/sorry/` не растёт. Иначе не выкатывать.

---

### P1. Bounded admission control (не копить задачи, которые уже никому не нужны)

**Проблема.** `prefetch_count=1` (`src/worker/worker.py:67`) сериализует работу, но не ограничивает
ожидание уже опубликованных задач. MCP отдаёт клиенту timeout, а задача всё равно доезжает до браузера и
жжёт профиль впустую.

**Откуда тянуть логику.**
[`limiter.ts` — overflow *отклоняет*, а не блокирует, и считает честный `retryAfterMs`](https://github.com/HarimxChoi/google-surf-mcp/blob/b331d06fe261e62b1badc7575da542cb952bee16/src/limiter.ts)
— ключевая идея в комментарии первых строк: отказ должен уложиться внутрь MCP call timeout.

**Как делать у нас.** In-process лимитер копировать буквально нельзя — настоящая очередь живёт в RabbitMQ.
Вариант: deadline прямо в `Task` (`src/contract.py:6-10`) — воркер просроченное не исполняет; либо
ограничение длины очереди на worker route. Переполнение и просрочка возвращаются как `rate_limited` /
`queue_deadline` с `retry_after_ms`.

---

### P1. Типизированные ошибки через весь тракт

**Проблема.** Воркер лепит строки (`src/worker/worker.py:38-57`, вплоть до
`f"processing_error: {error}"`), сервис распознаёт несколько литералов
(`src/api/service.py:174-195`), MCP часть случаев схлопывает в общий «попробуйте ещё раз»
(`src/api/mcp.py:234-251`). Клиент не может отличить «подожди и повтори» от «бесполезно повторять».

**Откуда тянуть логику.**

- [`types.ts` — форма `ErrorInfo {code, message, retryable, retry_after_ms, user_action, request_id, phase}`](https://github.com/HarimxChoi/google-surf-mcp/blob/b331d06fe261e62b1badc7575da542cb952bee16/src/types.ts#L100-L133)
- [`response.ts` — маппинг исключений в коды](https://github.com/HarimxChoi/google-surf-mcp/blob/b331d06fe261e62b1badc7575da542cb952bee16/src/response.ts#L25-L78)

**Осторожно.** У них половина классификации — regex по тексту исключения (`/timeout/i`, `/429|rate.?limit/i`).
Брать форму контракта, а не способ её заполнения: коды должны подниматься из типов исключений
(`src/worker/browser.py:247,251,261,272` уже дают основу), а не из `str(error)`.

**Нужные коды минимум:** `blocked`, `profile_exhausted`, `answer_refused`, `answer_unavailable`,
`navigation_timeout`, `queue_deadline`, `tab_not_found`, `human_action_required`.

---

### P1. Честный readiness и низкокардинальная телеметрия

**Проблема.** `/health` возвращает статическое `{"status": "ok"}` (`src/api/gateway.py:53-55`) — он не
падает, даже если AMQP лежит, воркер мёртв или браузер не поднялся. То есть как readiness-проба он
бесполезен.

**Откуда тянуть логику.**

- [`healthTool` — состав снимка: лимитер (`queueSize`, `recentCount`), пул, счётчики капч, конфиг](https://github.com/HarimxChoi/google-surf-mcp/blob/b331d06fe261e62b1badc7575da542cb952bee16/src/agent.ts#L1127-L1175)
- [`telemetry.ts` — типы событий и jsonl с ротацией](https://github.com/HarimxChoi/google-surf-mcp/blob/b331d06fe261e62b1badc7575da542cb952bee16/src/telemetry.ts)

**Что вынести у нас:** состояние AMQP, наличие живого reply от воркера, готовность браузера/профиля,
число активных вкладок, queue wait, длительность browser-фазы, коды результатов, ротации, block rate.
Полные запросы, cookies и URL с параметрами — не логировать.

**Оговорка.** У них health — диагностический snapshot, а не активная проба Google. Так и надо: проба
Google на каждый `/health` сама себя накажет.

---

### P2. Точнее классифицировать CAPTCHA и «AI Mode недоступен»

**Проблема.** `_check_blocked` (`src/worker/browser.py:288-289`) смотрит только на `/sorry/` в URL.
Варианты капчи без этого редиректа проходят мимо и вылезают дальше как невнятный timeout.

**Откуда тянуть логику.**
[`search.ts` — список паттернов блокировки и DOM-проверка на recaptcha](https://github.com/modelcontextprotocol-servers/google-search-mcp/blob/94afd22a74159f8f3309e350a6ab578649fd19ab/src/search.ts#L395-L410)
— единственное, что стоит взять из этого репозитория.

Отдельно распознавать «AI Mode недоступен для региона/языка», чтобы не отдавать generic
`answer_unavailable`.

---

### P2. Усилить критерий завершения ответа

Текущая стабилизация текста разумна как основной сигнал, но её стоит дополнить проверенным terminal UI
marker (блок feedback-контролов) и fixture-тестами на дампах. Чужие локализованные селекторы буквально не
переносить. Истечение `STREAM_TIMEOUT` (`src/worker/browser.py:113`) не считать успехом — timeout остаётся
явной ошибкой, а не разрешением вернуть половину ответа.

---

### P2. Ограниченный retry навигации

Retry первого `goto` (`src/worker/browser.py:743`) — только на временный network/5xx сбой, с общим
deadline, backoff+jitter и без повторной отправки уже принятого Google запроса. CAPTCHA и неоднозначный
timeout не ретраить автоматически.

---

## Что решено НЕ брать

Обоснование — в [разборе](research/google-mcp-comparison-2026-09-12.md), раздел «Что не брать». Коротко:

- query optimization из `google-ai-mode-mcp` — это MCP prompt, а не серверная логика, плюс хардкод года;
- их же «multi-agent / follow-up» — у нас реализовано существенно полнее;
- ручное решение капчи в headed-браузере — для VPS-фермы pool/cooldown/VNC лучше;
- 24-часовой кэш ответов Surf — конфликтует со свежестью AI Mode и живым `tab_id`;
- RAG / knowledge graph / Scholar / PDF-extraction Surf — другой продукт, чужой dependency tax;
- adaptive selector reordering без семантической проверки — молча неверный блок хуже явного падения;
- усечение ответа по умолчанию — сначала измерить реальные размеры;
- per-request locale/region — рвёт постоянный fingerprint профиля.
