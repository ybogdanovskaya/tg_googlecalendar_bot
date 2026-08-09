# Этап 8.8 — production-выпуск и Telegram UAT

Статус: завершён 9 августа 2026 года.

## Выпуск

- production PR `dev` → `main` прошёл обязательные проверки и был merged после подтверждения владельца;
- перед deploy GitHub Actions повторно проверил Python-кандидат, frontend и статическую сборку;
- production workflow в GitHub Environment `production` был вручную одобрен владельцем;
- успешный deploy выполнен для revision `4c73b3659cd3e20f6392a7ce6569051c0686d33f`;
- API Mini App работает как `calendar-miniapp.service` на `127.0.0.1:8001`;
- Nginx отдаёт статический frontend по `https://calendar.yibogdanovskaya.ru` и проксирует `/api/` только на loopback API;
- основной чат-бот, API Mini App и изолированный test-preview активны одновременно.

## Исправление во время выпуска

Две первые попытки workflow безопасно остановились до обновления production-кода: технический пользователь `calendarbot` не мог прочитать `requirements.txt` из root-only временного release-каталога при подготовке новой virtualenv.

Исправление внесено отдельным проверенным PR: staging-каталог остаётся под контролем `root`, а группе `calendarbot` выдан только доступ на чтение и проход по каталогам. На VPS это проверено в изолированной временной папке: чтение доступно, запись запрещена, папка удалена. Затем restricted server-side deploy entrypoint обновлён до этой версии с резервной копией предыдущей версии.

## Production-проверки

- GitHub Actions production workflow завершился успешно;
- revision на VPS соответствует выпущенному commit;
- `calendar-bot.service`, `calendar-miniapp.service` и `calendar-miniapp-preview.service` имеют статус `active`;
- публичные `https://calendar.yibogdanovskaya.ru/` и `/api/v1/health` отвечают `200`;
- Nginx-конфигурация проходит `nginx -t`;
- запрос к `/.env` получает `403`; скрытые файлы не выдаются;
- production-кнопка BotFather «Открыть календарь» назначена для основного бота.

## Telegram UAT владельца

Внутри основного Telegram-бота подтверждены:

1. открытие Mini App кнопкой «Открыть календарь»;
2. серверная Telegram-авторизация и корректное отображение имени владельца;
3. пользовательский сценарий выбора длительности, даты и свободных слотов без создания встречи;
4. административный экран владельца, статистика и статус Google Calendar;
5. отсутствие содержимого личного Google Calendar в пользовательском интерфейсе.

Тестовая встреча в production в рамках этого UAT не создавалась, поэтому календарные данные не изменялись.

## Эксплуатация

- Production Mini App предназначена для запуска из Telegram; прямой браузерный вход без Telegram `initData` не предоставляет доступ к данным.
- `calendar-dev.yibogdanovskaya.ru` и тестовый бот сохранены как изолированный preview для будущих проверок. Их не нужно удалять, пока не появится отдельное решение об их выводе из эксплуатации.
- Для следующей функциональной доработки используется обычный маршрут `feature/*` или `dev` → CI/UAT → PR → `main`; production workflow остаётся ручным и защищённым Environment approval.

