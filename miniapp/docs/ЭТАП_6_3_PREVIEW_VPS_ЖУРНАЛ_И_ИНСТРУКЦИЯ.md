# Этап 6.3 — изолированный preview на VPS

Статус: технический preview на VPS развёрнут и проверен; HTTPS URL назначен тестовому боту в BotFather; первая часть Telegram UAT пройдена. Следующая часть — функциональная UAT записи и администрирования. Production, `main`, рабочий бот, рабочая база и существующие сайты не изменяются.

## Контур preview

| Ресурс | Preview |
| --- | --- |
| Домен | `calendar-dev.yibogdanovskaya.ru` |
| Код | `/opt/calendar-miniapp-preview` |
| Статика | `/opt/calendar-miniapp-preview/static` |
| API | `127.0.0.1:8002` |
| systemd | `calendar-miniapp-preview.service` |
| База | `/var/lib/calendar-miniapp-preview/calendar_preview.sqlite3` |
| Журнал | `/var/log/calendar-miniapp-preview/calendar_preview.jsonl` |
| Конфигурация | `/etc/calendar-miniapp-preview/calendar-miniapp-preview.env` |

Рабочий контур остаётся в `/opt/calendar-bot`, `/var/lib/calendar-bot` и `calendar-bot.service`.

## Какие локальные значения понадобятся на VPS

Ни одно значение не передаётся в Git или чат. Владелец вводит их непосредственно в закрытые файлы VPS:

| Локальный ключ в `.env` | Файл или поле preview на VPS |
| --- | --- |
| `PREVIEW_BOT_TOKEN` | `/etc/calendar-miniapp-preview/telegram.token` |
| `ADMIN_TELEGRAM_ID` | поле `ADMIN_TELEGRAM_ID` в preview `.env` |
| `PREVIEW_APPS_SCRIPT_URL` | `/etc/calendar-miniapp-preview/google_apps_script.url` |
| `PREVIEW_APPS_SCRIPT_SECRET` | `/etc/calendar-miniapp-preview/google_apps_script.secret` |

`PREVIEW_GOOGLE_CALENDAR_ID` на VPS не нужен: тестовый Apps Script уже привязан к тестовому календарю. Рабочие `BOT_TOKEN`, `APPS_SCRIPT_URL`, Google token и личный календарь никогда не используются в preview.

## Порядок безопасного запуска

1. Создать отдельные каталоги, виртуальное окружение Python 3.12 и пустую SQLite preview-базу.
2. Поместить собранную статическую Mini App из ветки `dev` и исходный код этой же ревизии.
3. Создать закрытые preview-файлы со значениями из таблицы выше, не меняя production-файлы.
4. Установить bootstrap-конфигурацию Nginx и выпустить отдельный TLS-сертификат.
5. Установить финальную конфигурацию Nginx и новую systemd-службу, проверить `nginx -t` до graceful reload.
6. Проверить HTTPS, `/api/v1/health`, loopback-привязку API и изоляцию базы.
7. Только после этого назначить HTTPS URL тестовому боту в BotFather и пройти UAT.

## Откат

Если preview не запускается, выключается и удаляется только `calendar-miniapp-preview.service` и его Nginx server block. `calendar-bot.service`, существующие сайты, `/opt/calendar-bot`, production SQLite и сертификаты других доменов не изменяются.

## Журнал

- 2026-08-09: подтверждено отдельное разрешение владельца на создание изолированного preview-контура на VPS.
- 2026-08-09: проверены предпосылки VPS: Python 3.12, Certbot и Nginx установлены; `calendar-bot.service` активен; порт `127.0.0.1:8002` свободен; DNS-имя preview разрешается на VPS.
- 2026-08-09: добавлены не содержащие секретов шаблоны systemd и Nginx для preview.
- 2026-08-09: на VPS созданы самостоятельные preview-каталоги, Python 3.12 virtualenv, SQLite/log-каталоги и закрытые конфигурационные файлы. Production-каталоги, база и конфигурация не менялись.
- 2026-08-09: выпущен отдельный TLS-сертификат для `calendar-dev.yibogdanovskaya.ru`; Nginx-конфигурация прошла `nginx -t` до reload. HTTPS-статика отвечает `200`, запросы к скрытым файлам блокируются.
- 2026-08-09: запущена и включена в автозапуск отдельная `calendar-miniapp-preview.service`. Она слушает только `127.0.0.1:8002`.
- 2026-08-09: health-check доступен локально и через HTTPS; неавторизованный запрос к API возвращает `401`; безопасная проверка test Apps Script успешно прочитала занятость пустого test Calendar без создания встреч.
- 2026-08-09: HTTPS URL preview назначен только отдельному тестовому Telegram-боту через BotFather. Рабочий бот не менялся.
- 2026-08-09: при первом запуске в Telegram выявлено отсутствие официального Web Apps JavaScript bridge в HTML. Bridge добавлен до React, frontend проверен и preview-статика обновлена с сохранением предыдущей версии для отката.
- 2026-08-09: после cache-busting повторный запуск в Telegram успешно прошёл серверную проверку signed `initData`; пользовательский экран и серверная роль `ADMIN` доступны. Встречи на этой проверке не создавались.
- 2026-08-09: функциональная UAT подтверждена: пользовательская заявка создана и получила активный резерв, администратор согласовал её в Mini App, а сервер создал событие только в изолированном test Calendar. В интерфейсе списка заявок исправлены интервалы между действиями на узком экране.
- 2026-08-09: при UAT переноса подтверждено, что запрос сохраняется в preview-базе со статусом ожидания, а исходное событие не меняется до решения администратора. Исправлен интерфейс: пользователь видит статус открытого запроса и не может отправить повторный запрос до решения.
