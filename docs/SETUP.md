# Установка и настройка

Все настройки — в `.env` (образец: `.env.example`). Сам `.env`, ключи и база в git не попадают.

## 1. Ключ GigaChat

1. Войдите на [developers.sber.ru](https://developers.sber.ru/studio) через Сбер ID и создайте проект **GigaChat API** (физлицо).
2. В настройках проекта нажмите «Получить ключ» и скопируйте **Ключ авторизации** (Authorization key). Его показывают один раз.
3. Впишите его в `.env` после `GIGACHAT_CREDENTIALS=` (без кавычек).

Проверить формат ключа, не показывая его:

```powershell
.venv\Scripts\python scripts\check_gigachat.py --offline
```

GigaChat работает только с корневым сертификатом Минцифры. Это публичный файл с сайта Госуслуг.
Бот использует его только для соединения с GigaChat и не ставит в систему:
`sh scripts/get_certs.sh` (Git Bash), в Docker он скачивается сам.

Модели перечислены в `GIGACHAT_MODELS`. Когда у первой кончается бесплатный пакет токенов,
бот сам переходит к следующей. Полная проверка (токен, список моделей, вызов функции каждой моделью):

```powershell
.venv\Scripts\python scripts\check_gigachat.py
```

## 2. Google Таблица

1. [console.cloud.google.com](https://console.cloud.google.com) → создайте проект. Платёжные данные не нужны.
2. «APIs & Services» → «Library» → **Google Sheets API** → «Enable».
3. «IAM & Admin» → «Service Accounts» → «Create service account», роли не нужны.
4. Откройте аккаунт → «Keys» → «Add key» → «Create new key» → **JSON**. Сохраните файл как `secrets/google-service-account.json`.
5. Создайте Google-таблицу. «Настройки доступа»:
   - добавьте email сервисного аккаунта (`...@...iam.gserviceaccount.com`) с правом **Редактор**;
   - «Все, у кого есть ссылка» — **Читатель**: это и есть публичная витрина.
6. Скопируйте ID из адреса `docs.google.com/spreadsheets/d/<ID>/edit` в `GOOGLE_SHEET_ID`.

Лист «Записи» с заголовками бот создаст сам (пустой «Лист1» в новой таблице он переименует).
Если таблица недоступна, записи не теряются: они лежат в базе и догрузятся, когда доступ появится.
Ссылка для кнопки «Посмотреть в Google Таблице» — `SHEET_PUBLIC_URL` (например, с `#gid=` нужного листа).

## 3. Telegram

Создайте бота у [@BotFather](https://t.me/BotFather) и впишите токен в `TELEGRAM_BOT_TOKEN`.
Бот сам забирает сообщения (long polling): вебхук, домен и открытые порты не нужны.
Аватар и приветственная картинка — `python scripts/make_telegram_images.py` → `assets/telegram/`.

## 4. Запуск на своём компьютере

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python scripts\dev_server.py --fake
```

`--fake` — заглушка вместо модели, Telegram и посредник выключены: проверить виджет, базу и таблицу без GigaChat.
Без флага бот работает с GigaChat. Демо-страница: http://127.0.0.1:8000.

Прогнать диалоги через настоящую модель без сайта (записи — во временную базу):

```powershell
.venv\Scripts\python scripts\try_dialog.py --scenario smoke
```

## 5. Сервер

**Без root (как работает демо).** Бот запускается службой systemd от обычного пользователя
и перезапускается сам после сбоя и перезагрузки. Адрес сервера — в `.deploy.env` (он не попадает в git):

```bash
echo "VPS=user@host" > .deploy.env
bash scripts/deploy_user.sh        # залить код, поставить зависимости, перезапустить
bash scripts/server_ctl.sh logs    # на сервере: логи, status, restart
```

`.env` уходит на сервер только при первой выкладке, дальше его правят на сервере.

**Docker.** `bash scripts/deploy.sh` — то же через `docker compose`, `--https` добавляет Caddy с сертификатом
(нужна A-запись домена на сервер). Для nginx — образец в `deploy/nginx.conf`.

## 6. Чат на сайте

Плавающая кнопка чата в углу:

```html
<script src="https://bot.example.ru/widget.js" defer></script>
```

Чат прямо в блоке страницы:

```html
<div id="ai-chat" style="height:600px"></div>
<script src="https://bot.example.ru/widget.js" data-target="#ai-chat" defer></script>
```

Оформление под сайт: `data-accent`, `data-accent-ink`, `data-font`, `data-theme`; `data-open="true"` — открыть сразу.
В `CORS_ORIGINS` укажите адрес сайта, чтобы API принимал запросы только с него.

**Сайт на обычном хостинге, бот без домена.** Положите `deploy/php-relay` на хостинг сайта в папку `bot-api`,
скопируйте `_data/config.example.php` в `_data/config.php` и впишите ключ. На сервере бота укажите
`SITE_RELAY_URL=https://ваш-сайт/bot-api/` и тот же ключ в `SITE_RELAY_TOKEN`. Виджет подключается с
`data-api="/bot-api"`: сообщения ждут в очереди на хостинге, а бот сам за ними приходит.
На сервере бота не нужны ни открытые порты, ни HTTPS.

## 7. Другая ниша

Всё, что видит клиент, — в `config/business.yaml`: название, услуги с длительностью и ценой,
часы работы, число мест (`posts`), запчасти с вариантами, справочник неисправностей, вопросы-ответы
и дополнительные поля записи (`extra_fields`, например «Автомобиль» для автосервиса или «Мастер» для салона).
Колонки Google Таблицы строятся по этим полям.

`demo_busy_ratio` — доля «занятых» окон, чтобы расписание демо выглядело живым.

## 8. Защита от злоупотреблений

Публичное демо будут пытаться прогнать скриптами. Лимиты в `.env`: сообщений в минуту и в день с IP,
новых диалогов с IP, сообщений в диалоге, записей с IP в день, общий дневной лимит ходов модели.
IP хранится только в виде хэша. Бесплатный GigaChat обрабатывает один запрос за раз — остальные
ждут в очереди до `LLM_QUEUE_TIMEOUT` секунд.

## 9. Другая модель

Бот работает с моделью через интерфейс `LLMProvider` (`app/llm/base.py`). Чтобы добавить, например, DeepSeek,
нужен класс с методом `chat()` поверх его OpenAI-совместимого API (функции → `tools`) и строка
в `create_llm()` в `app/main.py`. Логика записи, промпт и виджет не меняются.
