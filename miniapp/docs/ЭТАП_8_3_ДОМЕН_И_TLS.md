# Этап 8.3 — домен и TLS-подготовка production Mini App

Статус: завершён 9 августа 2026 года. Это инфраструктурная подготовка, а не выпуск Mini App: API и production HTTPS-сайт намеренно не запущены.

## Выполнено

| Область | Результат |
| --- | --- |
| DNS | Владелец создал A-запись `calendar.yibogdanovskaya.ru` на VPS. Распространение подтверждено через независимые public DNS resolvers. |
| ACME | Включён временный HTTP-only Nginx site, который отдаёт только `/.well-known/acme-challenge/`; остальные запросы получают `404`. |
| TLS | Получен отдельный сертификат Let's Encrypt для `calendar.yibogdanovskaya.ru`. Автоматическое продление настроено Certbot. |
| Future config | На VPS сохранены не содержащие секретов systemd/env/Nginx templates из `deploy/`. systemd unit загружен, но не enabled и не started; полный HTTPS Nginx site тоже не enabled. |
| Проверки | `nginx -t` успешен. Рабочий бот и preview Mini App активны. |

## Преднамеренное безопасное состояние

- `calendar-miniapp.service` существует как будущая конфигурация, но не запущен.
- Полный HTTPS site `calendar.yibogdanovskaya.ru.conf` сохранён, но не включён в Nginx.
- Включён только временный ACME site. Он не раздаёт статику и не открывает приложение.
- Production SQLite, Google Calendar, bot token, Apps Script secret и existing production env не читались и не менялись.
- `calendar-dev.yibogdanovskaya.ru` и его test-бот остаются отдельным preview-контуром и продолжают использоваться для безопасных тестов.

## Почему production Mini App ещё не открывается

До release-кандидата в `main` на VPS нет выпущенной production-статики и backend-кода Mini App из `dev`. Включение HTTPS-сайта или API сейчас могло бы показать пустой или неготовый сервис. Поэтому адрес подготовлен, но не опубликован как Mini App.

## Следующий безопасный этап

P2.4 — rehearsal на изолированной свежей копии production SQLite:

1. Создать проверяемую копию базы без остановки рабочего бота.
2. Прогнать migrations и API startup против копии, не используя production Google Calendar для записи.
3. Проверить, что bot/API могут безопасно использовать одну WAL-схему и что rollback готов.
4. Удалить только временную копию после проверки; production SQLite не изменять.

Лишь после успешного rehearsal можно будет отдельно согласовать PR `dev` → `main`, включение HTTPS-site/API и ручной production deploy.

## Лог этапа

- 2026-08-09: владелец создал DNS A-запись `calendar`; распространение подтверждено через public DNS.
- 2026-08-09: создан временный ACME-only Nginx site, Nginx проверен и аккуратно перезагружен без перезапуска бот-сервисов.
- 2026-08-09: Certbot получил отдельный сертификат `calendar.yibogdanovskaya.ru` и включил автоматическое продление.
- 2026-08-09: future templates systemd/env/Nginx сохранены, но API и полный HTTPS site оставлены неактивными. Итоговая проверка: bot active, preview active, `nginx -t` successful.
