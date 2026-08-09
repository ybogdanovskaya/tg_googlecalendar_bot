# Этап 8.6 — server-side restricted deploy entrypoint

Статус: завершён 9 августа 2026 года. Это не выпуск Mini App и не перенос в `main`.

## Что проверено

На VPS фактически используется root-owned entrypoint `/usr/local/sbin/calendar-bot-deploy-from-stdin`. Право на его запуск предоставлено только техническому пользователю `caldeploy` через отдельное правило `/etc/sudoers.d/calendar-bot-deploy`.

До этого этапа entrypoint был старой версией: он не поддерживал статические файлы Mini App и безопасную смену Python virtualenv при изменении `requirements.txt`.

## Выполненное обновление

1. Версионная версия скрипта из `dev` прошла локальные проверки, полный Python/frontend test suite и GitHub CI.
2. Перед заменой на VPS выполнена проверка `bash -n` нового файла.
3. Старая серверная версия сохранена как `/usr/local/sbin/calendar-bot-deploy-from-stdin.backup-20260809T184353Z` с исходными правами `root:root`, `0750`.
4. Новый файл установлен с теми же правами и повторно проверен `bash -n` и `cmp`.
5. SHA-256 установленного файла совпадает с версией из `dev`: `eac830d8d3db2750f61a1e97c0d648c3e6ba2c4cd3cc3f39a13cb36ebbf64479`.

Скрипт также сделан обратно совместимым: обычный старый release-архив без `miniapp/dist` и без каталога `deploy` всё ещё обновляет только чат-бот, не удаляя существующие статические файлы Mini App или server-конфигурацию. Release-архив с Mini App обновляет и их.

## Проверка после изменения

- `calendar-bot.service` остался `active`;
- `calendar-miniapp-preview.service` остался `active`;
- preview API health-check прошёл;
- Nginx, production SQLite, production-домен Mini App, BotFather production-настройки и `main` не менялись;
- службы не перезапускались в рамках замены entrypoint.

## Следующий шаг

Технические prerequisite для будущего выпуска подготовлены. Следующий этап должен быть отдельным решением владельца о финальном production preflight и только после него — о merge/release Mini App из `main`.

