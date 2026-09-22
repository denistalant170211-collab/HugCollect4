# HugCollect4 / DarkCollect V9

## Files

- `bot.py` — основной Telegram-бот на базе текущего DarkCollect V8.
- `requirements.txt` — зависимости.
- `render.yaml` — Blueprint для Render.
- `.env.example` — список переменных для локального запуска.
- `.gitignore` — в том числе защита от случайного коммита Telegram `.session`.

Папку `assets/` из существующего репозитория оставьте без изменений.

## Что добавлено

### Капча

Капча проверяет пользователя один раз. После успешной проверки значение `captcha_verified_at` сохраняется в PostgreSQL. Награда/кредит за прохождение больше не выдаётся.

### Внутряк

Добавлен интерфейс:

1. Бот / Канал / Группа.
2. Публичный / Приватный.
3. `@username` или `t.me/...`.
4. Полный набор текущих Telegram ReportReason-категорий.
5. Наш шаблон или свой текст.
6. Сохранение задания в `internal_jobs`.
7. Просмотр собственных заданий.

Модуль не выполняет массовую рассылку жалоб с нескольких пользовательских аккаунтов.

### Session Manager

Администратор может:

- получить список `.session` из приватного Supabase Storage bucket;
- временно скачать сессию в `/tmp`;
- проверить авторизацию через Telethon;
- получить username и ID аккаунта;
- сохранить только метаданные проверки в PostgreSQL;
- удалить локальную копию после проверки.

Сами `.session` не сохраняются в PostgreSQL.

## Supabase

Создайте private bucket с именем `internal-sessions` и загрузите `.session` прямо в корень bucket.

Пример:

```text
internal-sessions/
├── account_001.session
├── account_002.session
└── account_003.session
```

Не публикуйте `.session`, `SUPABASE_SERVICE_ROLE_KEY`, `BOT_TOKEN`, `DATABASE_URL`, `TELEGRAM_API_HASH`.

## Render Environment

Добавьте:

```text
BOT_TOKEN=...
DATABASE_URL=...
ADMIN_IDS=...
MIRROR_ENCRYPTION_KEY=...
SUPPORT_USERNAME=@support

SUPABASE_URL=https://YOUR_PROJECT.supabase.co
SUPABASE_SERVICE_ROLE_KEY=...
SUPABASE_SESSIONS_BUCKET=internal-sessions

TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
INTERNAL_SESSION_CACHE_DIR=/tmp/darkcollect_sessions
```

`DATABASE_URL` — это существующее подключение PostgreSQL/Supabase. Его не нужно заменять на Storage URL.

## Render

Для существующего сервиса проще:

1. заменить `bot.py`;
2. заменить `requirements.txt`;
3. при желании использовать `render.yaml` как Blueprint;
4. добавить Environment Variables;
5. сделать Manual Deploy.

Persistent Disk для этого проекта не требуется: `.session` берутся из Supabase Storage и временно распаковываются в `/tmp`.

## Локальная проверка

```bash
python -m py_compile bot.py
```

Для запуска:

```bash
pip install -r requirements.txt
python bot.py
```
