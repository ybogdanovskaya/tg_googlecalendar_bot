# Этап 7 — готовность к production и план выпуска

Статус: подготовлен 9 августа 2026 года. Это план и итоговый чек-лист; он не является разрешением на merge в `main`, изменение production-сервера, рабочего бота, рабочей SQLite-базы или личного Google Calendar.

## 1. Итог текущего preview

| Область | Результат |
| --- | --- |
| Изоляция | Preview использует отдельные HTTPS-домен, test-бот, Apps Script, SQLite, systemd-службу и Nginx block. Рабочий бот и production-данные не использовались. |
| Telegram auth | Настоящее signed `initData` проверено сервером; роль `ADMIN` определяется сервером. |
| Пользователь | Пройдены создание заявки, согласование, перенос, отмена, список своих встреч и понятные статусы. |
| Администратор | Пройдены согласование, ручная встреча, редактирование, отмена, серия, перенос и отмена одного повторения, отмена серии, настройки и закрытые даты. |
| Google Calendar | Все UAT-действия проверены только на test Calendar; отменённые тестовые события и серия очищены. |
| Безопасность | `initData`, серверная роль, CSRF, ownership, idempotency и запрет выдачи содержимого личного Calendar покрыты API-тестами. Секреты не попали в Git. |
| Финальная проверка | Успешны 92 Python-теста, 6 frontend-тестов, production-сборка статики и проверка секретов. Preview HTTPS и health-check отвечают, preview и рабочий бот активны. |
| CI | Последний workflow `CI` для `dev` завершился успешно на commit `ab40862`. |

## 2. Чек-лист готовности к выпуску

### Уже выполнено

- [x] Static frontend: React/TypeScript/Vite; на VPS не нужен Node.js runtime или Docker.
- [x] Backend использует общую бизнес-логику и SQLite, а не вторую независимую систему.
- [x] Mini App не раскрывает пользователю содержание личного Google Calendar владельца.
- [x] Настоящий Telegram preview проверен с подписанными данными запуска.
- [x] Ветка `dev` содержит проверенные изменения; `main` не менялась.
- [x] CI проверяет Python и Mini App (`npm ci`, tests, build) на GitHub-hosted runner.
- [x] У preview есть отдельный health-check, TLS, журнал и сохранённые предыдущие статические версии для локального отката preview.

### Обязательные условия до production

- [x] Владелец авторизован в GitHub с правами администратора репозитория.
- [x] Создан GitHub Environment `production`.
- [x] В Environment добавлены только ограниченные secrets из [CI/CD инструкции](../../CI_CD_инструкция.md): `PROD_SSH_HOST`, `PROD_SSH_USER`, `PROD_SSH_PRIVATE_KEY`, `PROD_SSH_KNOWN_HOSTS`.
- [x] Для Environment назначен required reviewer — владелец.
- [x] Для `main` включена защита: merge только через pull request и обязательный успешный workflow `CI`.
- [x] Отдельно утверждён production-проект Mini App: статический bundle, ASGI API, Nginx reverse proxy и запуск службы на loopback. Код CD и templates подготовлены в `dev`, но ещё не применены на production; подробности — в [этапах 8.1](ЭТАП_8_1_PRODUCTION_МАРШРУТ.md) и [8.2](ЭТАП_8_2_CD_И_КОНФИГУРАЦИЯ.md).
- [ ] Production-схема и обратно совместимые миграции проверены на свежей копии рабочей базы с backup и rollback rehearsal.
- [x] Выполнен end-to-end CD безопасной текущей версии рабочего бота через ограниченный deploy key.
- [ ] Обновлён внешний паспорт VPS после подтверждённой production-конфигурации.

## 3. Почему production пока не запускается

Текущий workflow `Deploy production` безопасно развёртывает только существующую Python-часть: его архив не содержит `miniapp/` и не строит статический bundle. Он также не устанавливает отдельную ASGI-службу и Nginx route Mini App. Это правильное ограничение: попытка выпуска в текущем виде привела бы к неполному развёртыванию.

Для выпуска Mini App понадобятся согласованные изменения вне `miniapp/` — прежде всего в `.github/workflows/deploy-production.yml`, `deploy/`, production systemd/Nginx-конфигурации и паспорте VPS. Их нельзя выполнять в рамках этого подготовительного этапа без отдельного подтверждения владельца.

## 4. Рекомендуемый безопасный маршрут выпуска

### Шаг P1 — авторизация и защита GitHub

Владелец входит в GitHub, создаёт Environment `production`, добавляет четыре ограниченных SSH secret и назначает себя reviewer. Затем включает защиту `main` и обязательный check `CI`. Личный SSH-ключ, Telegram token, Apps Script URL/secret и Google credentials в GitHub не добавляются.

**Результат:** ручной deployment будет технически защищён и потребует явного подтверждения владельца.

### Шаг P2 — проектирование production CD Mini App

До каких-либо server changes фиксируются:

1. production-домен `calendar.yibogdanovskaya.ru`, DNS и отдельный TLS-сертификат;
2. root-owned расположение static artifact, отдельная ASGI-служба на loopback и Nginx routes `/` и `/api/`;
3. единая production SQLite с WAL и план совместной работы бота и API;
4. серверные закрытые файлы конфигурации — только на VPS, без значений в GitHub или Git;
5. backup, health-check и откат к предыдущему коду и базе.

**Стоп-условие:** если хотя бы один пункт не проверен на копии базы, production не меняется.

### Шаг P3 — реализация и изолированная проверка production-кандидата

В `feature/miniapp-production-*` вносятся минимальные изменения CD и deploy-конфигурации. Они проходят CI, статическая сборка публикуется только как deployment artifact, а серверный сценарий повторяет проверки до переключения версии. Затем проверяется кандидат на изолированном окружении или актуальной копии production-данных без доступа пользователей.

**Стоп-условие:** ошибки health-check, миграции, Google gateway или несовместимость с ботом запускают rollback; сайт и Nginx не перезапускаются ради обычного обновления бота.

### Шаг P4 — controlled production release

Только после отдельного текстового подтверждения владельца:

1. создаётся pull request `dev` → `main`;
2. CI успешно завершается на точном commit-кандидате;
3. владелец подтверждает merge;
4. из `main` вручную запускается `Deploy production` и подтверждается GitHub Environment;
5. после deployment проверяются bot service, Mini App API, HTTPS, logs, backup и Google gateway;
6. при любой проблеме выполняется предусмотренный rollback.

## 5. Роли и секреты

| Где хранится | Что допустимо | Что запрещено |
| --- | --- | --- |
| Локальный `.env` | локальные и тестовые значения владельца | commit или пересылка значений в чат |
| Закрытые файлы VPS | production bot/API/Google configuration | вывод содержимого в logs, Git или Actions |
| GitHub Environment `production` | только четыре ограниченных SSH deploy secrets | личный SSH key, bot token, Google token, Apps Script secret |
| Frontend bundle | только публичная конфигурация вроде `/api/v1` | любые credentials, Telegram token, ID владельца, URL/secret Google gateway |

## 6. Критерий решения «можно выпускать»

Переход к P4 допускается, только когда отмечены все обязательные пункты раздела 2, UAT preview остаётся успешной, CI green на точном кандидате `main`, а владелец отдельно подтвердил и merge, и запуск production deployment.

До этого Mini App остаётся доступной только в изолированном test preview, а рабочий чат-бот продолжает работать по прежнему маршруту.

## 7. Следующее действие владельца

P1 завершён. Следующее действие — отдельное подтверждение владельца на P2: проектирование и реализацию production-маршрута Mini App. До него `main` и production Mini App не меняются.

## Журнал P1

- 2026-08-09: создан GitHub Environment `production`; назначен required reviewer — владелец. Обход подтверждения администраторами выключен.
- 2026-08-09: для `main` включены pull request, обязательные checks `Python checks` и `Mini App checks`, актуальность ветки перед merge, обязательное разрешение обсуждений, запрет force-push и удаления ветки. Второй reviewer не требуется для личного проекта.
- 2026-08-09: владелец добавил в Environment четыре ограниченных SSH secrets: `PROD_SSH_HOST`, `PROD_SSH_USER`, `PROD_SSH_PRIVATE_KEY`, `PROD_SSH_KNOWN_HOSTS`. Проверены только имена и количество; значения не читались и не сохранялись в Git.
- 2026-08-09: end-to-end CD рабочего бота успешно завершён из `main` после отдельного ручного approval владельца в Environment. Проверены revision `c84d1be`, активность рабочего бота и preview, штатный health-check и preview health endpoint. Mini App в production не выпускалась.
- 2026-08-09: владелец подтвердил Telegram-only режим. P2.1 завершён: проект production-маршрута задокументирован; в ходе read-only проверки выявлено, что production DNS и Nginx site Mini App ещё не существуют. Никакие production-изменения не выполнялись.
- 2026-08-09: P2.2 завершён в `dev`: подготовлены CI/CD, systemd, Nginx и rollback-templates для Mini App. Production-инфраструктура, main и работающий бот не менялись.
- 2026-08-09: P2.3 завершён: DNS A-запись и TLS для будущего production-домена созданы, а конфигурации сохранены на VPS в неактивном состоянии. Рабочий бот, production SQLite и preview не менялись; подробности — в [этапе 8.3](ЭТАП_8_3_ДОМЕН_И_TLS.md).
- 2026-08-09: P2.4 завершён: Mini App API и migrations проверены на закрытой временной копии production SQLite, после чего все rehearsal-артефакты удалены. Выявлена необходимость отдельного безопасного этапа обновления Python virtualenv до production release; подробности — в [этапе 8.4](ЭТАП_8_4_REHEARSAL_КОПИИ_БАЗЫ.md).
- 2026-08-09: P2.5 завершён в `dev`: подготовлено и изолированно проверено безопасное создание/switch/rollback Python virtualenv при изменении зависимостей. До выпуска остаётся отдельно обновить server-side restricted deploy entrypoint; подробности — в [этапе 8.5](ЭТАП_8_5_БЕЗОПАСНОЕ_ОБНОВЛЕНИЕ_VENV.md).
- 2026-08-09: P2.6 завершён: restricted deploy entrypoint на VPS обновлён из проверенной ветки `dev` с серверной резервной копией, контрольной суммой и без перезапуска служб. Добавлена обратная совместимость с прежними пакетами обычного бота; подробности — в [этапе 8.6](ЭТАП_8_6_SERVER_SIDE_DEPLOY_ENTRYPOINT.md).
- 2026-08-09: P2.7 завершён как read-only production preflight. Инфраструктура, конфигурация, CI и rollback готовы; production Mini App намеренно остаётся выключенным до отдельного подтверждения релиза и управляемой активации Nginx/API/BotFather. Подробности — в [этапе 8.7](ЭТАП_8_7_PRODUCTION_PREFLIGHT.md).
