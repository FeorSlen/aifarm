# aifarm

Ферма Google AI Mode: FastAPI-шлюз с нативным **MCP-сервером** + воркеры на
анти-детект браузере (Camoufox), общение через RabbitMQ.

```
MCP client ──HTTP──> gateway ──AMQP──> worker ──Camoufox──> Google AI Mode
```

## Запуск

```bash
cp .env.example .env          # задать RABBITMQ_PASSWORD
docker compose up -d --build
curl -s localhost:8000/health # {"status":"ok"}

claude mcp add --transport http aifarm http://localhost:8000/mcp
```

Инструменты MCP: `search` (вопрос → ответ AI Mode, блокирующий), `my_searches`
(список вкладок), `close` (закрыть вкладку). `tab_id` из ответа `search`
передаётся обратно, чтобы продолжить тот же диалог.

## Публичный доступ (VPS)

```
GATEWAY_BIND=0.0.0.0:8000
AIFARM_MCP_HOST=0.0.0.0        # выключает DNS-rebinding guard
```
Аутентификации у сервера нет — только за файрволом (`ufw allow from <ip> to any port 8000`).

## Прокси

Без резидентного/мобильного прокси Google банит IP датацентра (`/sorry/`).
```
PROXY_URL=http://user:pass@host:port     # sticky-эндпоинт, ротация IP на стороне провайдера
```

## Тёплые куки

Холодный профиль + свежий IP = reCAPTCHA почти всегда. Куки живого браузера
резко снижают частоту. Экспорт: расширение Cookie-Editor (JSON) или
`Get cookies.txt LOCALLY` (Netscape). Положить файл и указать:
```
GOOGLE_COOKIES_FILE=/app/cookies.txt     # . смонтирован в /app
```
Залогиненные куки (`SID`, `SAPISID`, `__Secure-1PSID`…) убирают капчу почти
полностью, но **привязывают аккаунт к трафику фермы** — риск блокировки
аккаунта. Лучше левый аккаунт или только `NID`/`SOCS`/`AEC`/`DV`.

## Флаги (env, без ребилда)

| Переменная | Дефолт | Что делает |
|---|---|---|
| `PROXY_URL` | — | sticky-прокси; пусто = прямое подключение |
| `AIFARM_PROFILE_ROOT` | — | пул постоянных профилей `user1…userN`; профили с `.needs_attention` пропускаются |
| `AIFARM_PROFILE_COUNT` | `10` | число профилей в пуле |
| `AIFARM_PROFILE_PATH` | — | прямой путь к одному профилю; имеет приоритет над пулом |
| `GOOGLE_COOKIES_FILE` | — | путь к файлу куков (Netscape/JSON) |
| `AIFARM_SEED_COOKIES` | `true` | инъекция куков (файл + дефолтное согласие) до навигации |
| `AIFARM_ACCEPT_CONSENT` | `true` | авто-принятие диалога согласия Google (все фреймы/локали) |
| `AIFARM_BURN_ON_BLOCK` | `true` | вне пула при `/sorry/` закрыть контекст и выбрать новый профиль, сохранив старый; `false` — не переключать |
| `AIFARM_HUMANIZE` | `true` | человекоподобные движения курсора Camoufox |
| `AIFARM_GEOIP` | `true` | подгонка TZ/локали браузера под exit-IP прокси |
| `AIFARM_BLOCK_WEBRTC` | `true` | блок утечки реального IP через WebRTC |
| `AIFARM_DENOISE` | `true` | чистка ответа AI Mode от UI-обвязки (кнопки-цитаты, сноски) |
| `AIFARM_DUMP_ANSWER` | — | каталог для дампа ответа+скриншота/HTML при сбое (диагностика); каталог должен быть writable для uid 10001, напр. `/data/profiles/dump` |
| `GOOGLE_HEADLESS` | `false` | headless-режим Camoufox (воркер и так под Xvfb) |
| `NAV_TIMEOUT_MS` | `45000` | потолок навигации (goto/load-state) |
| `EL_TIMEOUT_MS` | `20000` | потолок ожидания элементов |
| `STREAM_TIMEOUT_S` | `45` | потолок стриминга ответа AI Mode |
| `AIFARM_SEARCH_TIMEOUT_S` | `180` | общий потолок инструмента `search` в шлюзе |
| `GATEWAY_BIND` | `127.0.0.1:8000` | привязка публикуемого порта |
| `AIFARM_MCP_HOST` | `127.0.0.1` | host для `/mcp`; не-loopback выключает DNS-rebinding guard |
| `AIFARM_USER_ID` | — | общий namespace-арендатор (uuid4) для клиентов без `X-Aifarm-User` |
| `WORKER_ID` | `worker1` | id воркера (маршрутизация в RabbitMQ) |

## Ручной прогрев профилей

Профили хранятся локально в `protected profile/profile/user1…user10`; каталог
игнорируется Git. Открыть профиль для ручной проверки:

```bash
python scripts/warm_profile.py 1
```

При `/sorry/` worker сохраняет текущий профиль, создаёт в нём маркер
`.needs_attention` и выбирает следующий готовый профиль. Заблокированный запрос
не повторяется автоматически. Когда готовых профилей нет, MCP возвращает
`human_action_required`. После ручной проверки скрипт предложит удалить маркер
и вернуть профиль в пул.

## Известные ограничения

- `_next_proxy()` — один sticky-эндпоинт из `PROXY_URL`, пула нет.
- Один воркер: `Gateway` хардкодит `WORKER_ID`; для нескольких нужен роутинг.
- Google `/sorry/` reCAPTCHA не решается — только обход куками/прокси.
- Денойз: структурный стрип; фильтр UI-строк по локалям не доделан (`sup` под вопросом).
