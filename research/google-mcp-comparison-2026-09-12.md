# Сравнение `aifarm` с тремя Google Search MCP

Дата проверки: 2026-09-12  
Локальный baseline: `aifarm` @ `5cb19a9`  
Внешние snapshots: `google-surf-mcp` @ `b331d06`, `google-ai-mode-mcp` @ `04eb0ba`, `google-search-mcp` @ `94afd22`

## Краткий ответ

Главный переносимый продуктовый элемент — структурированные цитаты из `google-ai-mode-mcp`: сохранить связь между местом сноски в ответе и списком источников, а в MCP вернуть `answer`, `citations` и дедуплицированный `sources`. Переносить их код буквально нельзя: он синхронно крутит busy-wait внутри `page.evaluate`, из-за чего браузерный main thread не может нормально обработать обновление sidebar.

Из `google-surf-mcp` полезнее всего не его огромный RAG/Scholar-комбайн, а три эксплуатационных контракта: ограничение очереди с `retry_after`, типизированные end-to-end ошибки и содержательный health/telemetry. Это напрямую закрывает слабые места текущей схемы gateway → RabbitMQ → один worker.

`modelcontextprotocol-servers/google-search-mcp` — маленький прототип обычной SERP, не конкурент AI Mode-ферме. Заимствовать из него архитектуру не стоит. Максимум — расширить распознавание CAPTCHA URL/DOM и добавить строго ограниченный retry только для навигационных сбоев.

## Сопоставление

| Возможность | `aifarm` | `google-ai-mode-mcp` | `google-surf-mcp` | `google-search-mcp` |
|---|---|---|---|---|
| Google AI Mode | Да | Да | Нет, обычная SERP/Scholar | Нет, обычная SERP |
| Настоящий follow-up | Да, живая вкладка + `tab_id` | Нет: новая страница на вызов | Не является целью | Нет |
| Масштабирование | Gateway + AMQP worker boundary, пока один route | Один singleton browser context | Локальный broker/pool, parallel search | Один процесс |
| Антибот-профили | Camoufox, постоянное устройство, пул, cooldown, нагрузочная ротация | Один persistent Patchright profile | Тёплый Chrome profile, cascade и human recovery | Непоследовательное сохранение state/fingerprint |
| Структурированные цитаты AI Mode | Нет, UI-сноски удаляются denoise | Да, но реализация хрупкая | SERP sources, не AI Mode citations | SERP links |
| Admission control | Нет явного bounded contract | Нет | Да, bounded wait + `retry_after_ms` | Нет |
| Typed errors/health | Частично, много string matching; `/health` поверхностный | Частично | Наиболее развито | Слабый error contract |
| Search/fetch/Scholar/RAG | Нет, вне текущей цели | Нет | Да, очень широкий продукт | Нет |

## Что брать

### P0 — структурированные цитаты AI Mode

`google-ai-mode-mcp` реально размечает citation buttons, кликает их и собирает ссылки из `rhs-col`, затем превращает маркеры в Markdown-сноски. В `aifarm` ответ сейчас возвращается как plain text, а denoise намеренно удаляет кнопки и `sup`.

Рекомендуемая форма для `aifarm`:

- клики выполнять Playwright-операциями по одному, вне `page.evaluate`;
- после каждого клика ждать mutation или смену нормализованного набора ссылок;
- хранить `citation -> sources`, плюс глобальный дедуплицированный список;
- возвращать `structured_content={tab_id, answer, citations, sources}`;
- при дрейфе DOM сохранять нынешний plain-text fallback, не валить весь поиск;
- сначала проверить на дампах нескольких локалей и на живой выборке.

Критерий остановки: если на 50+ ответах ручная проверка не даёт хотя бы 95% правильных соответствий при приемлемой задержке и без роста `/sorry/`, фичу не выпускать.

### P1 — bounded admission control

RabbitMQ `prefetch_count=1` сериализует фактическую работу, но не ограничивает время ожидания уже опубликованных задач. MCP может отдать timeout, после чего задача всё ещё доберётся до браузера и зря потратит профиль.

Нужно ограничить очередь по worker route или вводить deadline в `Task`: просроченное не исполнять, переполнение возвращать как `rate_limited`/`busy` с `retry_after_ms`. In-process limiter Surf копировать буквально не надо — реальная очередь находится в RabbitMQ.

### P1 — типизированные ошибки через весь тракт

Заменить строковое поле ошибки контрактом вроде `{code, message, retryable, retry_after_ms, user_action}`. Сейчас worker формирует строки, gateway распознаёт несколько литералов, а MCP часть случаев схлопывает в общий совет «попробуйте ещё раз». Типизация особенно нужна для `blocked`, `profile_exhausted`, `answer_refused`, `navigation_timeout`, `queue_deadline`, `tab_not_found`.

### P1 — честный readiness и низкокардинальная телеметрия

`/health -> {status: ok}` недостаточен. Полезны состояние AMQP, наличие живого worker reply, browser/profile readiness, активные tabs, queue wait, browser duration, codes результатов, rotations и block rate. Полные запросы, cookies и URL с чувствительными параметрами логировать не следует.

### P2 — точнее классифицировать CAPTCHA и unavailable

Расширить `_check_blocked` за пределы `/sorry/`: CAPTCHA/recaptcha DOM и известные redirect-варианты. Отдельно распознавать «AI Mode недоступен для региона/языка», чтобы не отдавать generic `answer_unavailable`.

### P2 — усилить критерий завершения ответа

Текущие 1,5 секунды неизменного текста разумны как основной сигнал, но их стоит дополнить проверенным terminal UI marker (feedback controls) и fixture-тестами. Не переносить буквально чужие локализованные селекторы и не считать истечение 40 секунд успешным завершением: timeout должен оставаться явной ошибкой, а не разрешением вернуть потенциально частичный ответ.

### P2 — ограниченный retry навигации

Допустим retry первого `goto` только для временного network/5xx failure, с единым deadline, backoff+jitter и без повторной отправки уже принятого Google запроса. CAPTCHA и неоднозначный timeout не ретраить автоматически.

## Что не брать

- Query optimization из `google-ai-mode-mcp`: это необязательный MCP prompt, а не серверная оптимизация. Обычный tool call передаёт query буквально. Hard-coded год и версии быстро протухают и могут менять намерение пользователя.
- Заявленный там multi-agent/follow-up: сервер имеет один browser manager, не имеет queue/mutex/session ID и закрывает страницу после каждого вызова. У `aifarm` follow-up реализован существенно полнее.
- Реализацию CAPTCHA из двух малых проектов: она означает ручное решение в headed browser, а у простого `google-search-mcp` нет даже корректного ожидания решения. Для VPS-фермы pool/cooldown/VNC лучше.
- 24-часовой cache ответов Surf: он конфликтует со свежестью AI Mode и живым `tab_id`. Если дубли окажутся проблемой, сначала сделать короткий in-flight single-flight, а не answer cache.
- RAG, knowledge graph, Scholar, fetch/PDF extraction Surf: это отдельный исследовательский продукт и большой dependency/operations tax, не улучшение ядра AI Mode-фермы.
- Adaptive selector reordering без семантической проверки: неверно выбранный непустой блок хуже явного сбоя со screenshot/HTML. Полезно заимствовать только fixtures, degradation counters и несколько независимых проверяемых стратегий, когда появятся реальные случаи дрейфа.
- Усечение ответа по умолчанию: сначала измерить размеры и убрать возможное двойное представление payload; если лимиты реальны, вводить явный `truncated` и continuation/storage handle.
- Per-request locale/region из простого SERP MCP: для постоянной личности смена locale на каждый запрос создаёт несогласованный fingerprint. Если это понадобится, locale должен быть свойством профиля/worker pool.

## Проверка заявлений проектов

`google-ai-mode-mcp` архивирован и прямо предупреждает, что больше не поддерживается. Citation extraction существует, но тестового набора в репозитории нет. «Query optimization» живёт только в prompt template. «Handles follow-up context» не подтверждается MCP API: единственный tool создаёт и закрывает новую страницу. «Multi-agent support» означает совместимость с MCP-клиентами, а не безопасную конкурентную серверную модель.

`google-search-mcp` имеет очень малую кодовую базу и не имеет тестов. Веб-страница GitHub на дату проверки показывает один commit, но локальный clone был shallow, поэтому полная история по нему отдельно не устанавливалась. Region вычисляется, но навигация идёт на hard-coded `www.google.com`; headed CAPTCHA branch бросает исключение вместо ожидания пользователя; fingerprint/state записываются и читаются в несовместимых формах. README в этих местах сильнее кода.

`google-surf-mcp` — зрелее двух остальных по тестам и эксплуатационной поверхности, но решает другой класс задачи. Его README подтверждает browser/SearchApi fallback, parallel search, extraction, Scholar, health и локальную project memory. Эти элементы нельзя считать доказательством, что такой комбайн нужен `aifarm`.

## Ограничения и уверенность

Сравнение основано на текущих исходниках и commit snapshots, а не на длительном live-тесте всех четырёх систем на одинаковых IP и профилях. Поэтому уверенность высокая для API/архитектурных фактов, средняя для реального влияния предлагаемых изменений на CAPTCHA rate и latency. Приоритет P1 может измениться после метрик production queue wait и block rate.

## Источники и происхождение

- [`aifarm` README и исходники](../README.md): локальный baseline; код и тесты на commit `5cb19a9`.
- [HarimxChoi/google-surf-mcp](https://github.com/HarimxChoi/google-surf-mcp): первичный source/README для Surf; commit `b331d06`, проверен 2026-09-12.
- [google-surf-mcp changelog](https://github.com/HarimxChoi/google-surf-mcp/blob/main/CHANGELOG.md): история limiter, pool fallback, CAPTCHA modes и research memory.
- [PleasePrompto/google-ai-mode-mcp](https://github.com/PleasePrompto/google-ai-mode-mcp): первичный source/README; commit `04eb0ba`, проверен 2026-09-12.
- [modelcontextprotocol-servers/google-search-mcp](https://github.com/modelcontextprotocol-servers/google-search-mcp): первичный source/README; commit `94afd22`, проверен 2026-09-12.

Все три внешних репозитория — независимые реализации. README и исходники внутри одного репозитория относятся к одной evidence chain; README-заявление не считалось подтверждённым, если ему противоречил код. Benchmark-заявления авторов не использовались для рекомендаций без воспроизводимого прогона в среде `aifarm`.

Аудит происхождения: фактический GitHub archive flag и состояние npm отдельно не проверялись; утверждение о прекращении поддержки `google-ai-mode-mcp` исходит из README и сообщения текущего коммита одной авторской цепочки. `google-surf-mcp` health — диагностический snapshot, а не активная end-to-end проба Google. Его structured errors полезны как контракт, хотя часть текущей классификации всё ещё построена на regex текста исключения.
