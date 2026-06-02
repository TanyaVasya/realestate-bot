# 🏠 Квартирный бот для Telegram

Бот, который живёт в вашем с мужем чате. Кидаете ссылку на квартиру с
realestate.com.au или domain.com.au — он заходит, собирает характеристики и
заносит в базу (Google Sheets). А ещё с ним можно говорить обычными словами:
«покажи всё, что ещё не смотрели», «отметь #3 как просмотрена, нам понравилась 9/10».

LLM под капотом — Claude.

---

## Что внутри

| Файл | Что делает |
|------|------------|
| `bot.py` | Точка входа, ловит сообщения в Telegram |
| `scraper.py` | Открывает страницу, собирает сырые данные |
| `llm.py` | Claude: вытаскивает характеристики + разговор |
| `sheets.py` | Чтение/запись в Google Sheets |
| `config.py` | Настройки из переменных окружения |

База в Google Sheets — вы оба можете открыть таблицу с телефона и править руками,
бот пишет в те же строки.

---

## Шаг 1. Создать бота в Telegram

1. Открой [@BotFather](https://t.me/BotFather) → команда `/newbot`.
2. Дай имя и username. Получишь **токен** вида `123456:ABC...` — сохрани.
3. **Важно для группового чата:** `/setprivacy` → выбери бота → **Disable**.
   Без этого бот не видит обычные сообщения в группе.
4. Добавь бота в ваш чат с мужем.

## Шаг 2. Ключ Claude

Возьми API-ключ в [console.anthropic.com](https://console.anthropic.com) → `sk-ant-...`.

## Шаг 3. База в Google Sheets

1. Создай пустую таблицу на [sheets.google.com](https://sheets.google.com).
   Из её адреса `docs.google.com/spreadsheets/d/`**`ЭТО_ID`**`/edit` скопируй ID.
2. Сделай **сервисный аккаунт** (бесплатно):
   - [console.cloud.google.com](https://console.cloud.google.com) → создай проект.
   - Включи **Google Sheets API** (APIs & Services → Enable APIs → найди Sheets).
   - APIs & Services → Credentials → **Create credentials → Service account**.
   - У созданного аккаунта: вкладка **Keys → Add key → JSON**. Скачается файл —
     переименуй в `credentials.json` и положи рядом с `bot.py`.
3. Открой `credentials.json`, скопируй email вида `...@...iam.gserviceaccount.com`
   и **поделись своей таблицей** с этим email (кнопка Share, права Editor).

## Шаг 4. Настройки

```bash
cp .env.example .env
```
Открой `.env` и впиши `TELEGRAM_TOKEN`, `ANTHROPIC_API_KEY`, `SHEET_ID`.

## Шаг 5. Запуск локально (проверить)

```bash
cd realestate-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

Кинь в чат ссылку на квартиру — бот должен ответить карточкой, а в таблице
появится строка.

---

## Шаг 6. Запуск 24/7 на облаке (Railway)

Локально бот работает, только пока включён Mac. Чтобы ловил сообщения всегда:

1. Залей папку в GitHub-репозиторий (без `.env` и `credentials.json` — они в `.gitignore`).
2. [railway.app](https://railway.app) → New Project → Deploy from GitHub repo.
3. В **Variables** добавь:
   - `TELEGRAM_TOKEN`, `ANTHROPIC_API_KEY`, `SHEET_ID`
   - `GOOGLE_CREDENTIALS_JSON` — **всё содержимое** `credentials.json` одной строкой
     (вместо файла; код умеет читать и так).
4. Railway сам подхватит `Procfile` и запустит `worker: python bot.py`.

Render / Fly.io работают так же — тип сервиса **Background Worker**, без портов.

---

## Как пользоваться

**Добавить квартиру** — просто кинь ссылку в чат. Дубликаты бот узнаёт.

**Спросить / изменить** — упомяни бота (`@имя_бота ...`) или ответь на его
сообщение, либо пиши в личку боту:

- «что мы ещё не ходили смотреть?»
- «какие до $800 в неделю в Bondi?»
- «отметь #4 как viewed, дата сегодня, оценка 8/10»
- «#2 — reject, далеко от работы»
- «добавь к #5 заметку: солнечная, но шумная улица»

Статусы: `new`, `interested`, `shortlisted`, `viewed`, `rejected`, `applied`.

---

## Если сайт блокирует парсинг

realestate.com.au защищён жёстко. Если бот часто пишет «не смог открыть
страницу» — заведи бесплатный ключ на [zenrows.com](https://www.zenrows.com)
и впиши `ZENROWS_API_KEY` в переменные. Код сам переключится на него, когда
обычный запрос не проходит.
