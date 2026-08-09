# Этап 6.2 — изолированный HTTPS preview для Telegram

Статус: проектирование. Этот документ не является разрешением на изменение DNS, BotFather, VPS, Nginx, systemd, GitHub secrets или production.

## Рекомендованная схема

```text
Тестовый Telegram Bot
        │  HTTPS + signed initData
        ▼
calendar-dev.yibogdanovskaya.ru
        │
        ├─ /       → статический Mini App из ветки dev
        └─ /api/   → calendar-miniapp-preview.service (127.0.0.1:8002)
                              │
                              ├─ отдельная SQLite preview-база
                              └─ отдельный тестовый Calendar gateway и календарь
```

`calendar-dev.yibogdanovskaya.ru` — рекомендуемое имя preview-поддомена. Production-домен `calendar.yibogdanovskaya.ru`, существующий сайт и бот остаются неизменными.

## Обязательная изоляция

| Компонент | Preview | Production |
| --- | --- | --- |
| Git | Только commit из `dev` | Только проверенный `main` |
| Telegram | Новый тестовый бот | Действующий бот |
| База | Отдельный файл `calendar_preview.sqlite3` | Текущая production SQLite |
| API | Отдельная ASGI-служба на `127.0.0.1:8002` | Новый сервис отсутствует до отдельного выпуска |
| Google Calendar | Новый пустой тестовый календарь | Личный календарь владельца |
| Google gateway | Отдельный test deployment и secret **или** отдельный OAuth token/test calendar | Текущий Apps Script gateway и secret |
| Конфигурация | Отдельный root-owned environment file | `/etc/calendar-bot/calendar-bot.env` без изменений |
| Nginx | Новый отдельный server block | Текущие server block без изменения маршрутов |

### Почему нельзя переиспользовать текущий Apps Script URL

`AppsScriptCalendar` посылает в gateway только действие и данные события; параметр calendar ID в запрос не передаётся. Значит, существующий `APPS_SCRIPT_URL` привязан к текущему личному календарю и никогда не должен использоваться для preview. Для preview нужен отдельный тестовый Apps Script deployment с отдельным секретом и настроенным тестовым календарём либо отдельный OAuth token, выданный только для тестового календаря.

## Предлагаемые серверные объекты

Ниже — проектные имена, их создание будет отдельным подтверждаемым действием.

- код: `/opt/calendar-miniapp-preview`;
- Python environment: `/opt/calendar-miniapp-preview/.venv`;
- статика: `/opt/calendar-miniapp-preview/static`;
- конфигурация: `/etc/calendar-miniapp-preview/calendar-miniapp-preview.env` (`root:root`, режим `0600`);
- тестовые secret-файлы: `/etc/calendar-miniapp-preview/` (`root:root`, режим `0600`);
- база: `/var/lib/calendar-miniapp-preview/calendar_preview.sqlite3`;
- журналы: `/var/log/calendar-miniapp-preview/`;
- systemd unit: `calendar-miniapp-preview.service` с `User=calendarbot`, `PrivateTmp=true`, `NoNewPrivileges=true`, ограничением памяти и loopback bind;
- Nginx upstream: `127.0.0.1:8002`.

Node.js на VPS не устанавливается и не запускается: сервер получает уже собранный статический `dist/`.

## Конфигурационные принципы preview

- `MINIAPP_API_BIND_HOST=127.0.0.1`, `MINIAPP_API_BIND_PORT=8002`, `MINIAPP_COOKIE_SECURE=true`;
- frontend собирается с `VITE_API_BASE=/api/v1`, поэтому cookie и API остаются same-origin;
- preview использует только test `BOT_TOKEN`, test `ADMIN_TELEGRAM_ID`, test Calendar gateway/token, test database и собственные log paths;
- в frontend bundle не попадают токены, URL gateway, Google secret, Telegram ID или путь базы;
- Nginx передаёт только `/api/` на loopback API и отдаёт `/` как статические файлы; прямой порт 8002 снаружи закрыт;
- новый server block проходит `nginx -t` до graceful reload; существующие bot/systemd-службы не перезапускаются.

## Кто и что должен подготовить

### Действия владельца во внешних сервисах

1. Создать тестовый Telegram-бот в BotFather, не меняя действующий бот.
2. Создать пустой тестовый Google Calendar.
3. Выбрать один из вариантов доступа к тестовому календарю: отдельный Apps Script deployment + secret (рекомендуется для проверки production-пути) либо отдельный OAuth token, доступный только к тестовому календарю.
4. Создать DNS-запись `calendar-dev` на VPS и подтвердить право выпускать TLS-сертификат.
5. Для полного UAT иметь второй Telegram-аккаунт: владелец тестового бота проверяет роль `ADMIN`, второй аккаунт — обычного пользователя.
6. Позже авторизоваться в GitHub для создания отдельного Environment `preview` и secrets ограниченного preview deploy key. Открытые вопросы Environment `production` при этом не меняются.

Значения токенов, secrets, URL gateway, Google token и личные ID не передаются в Git, документацию или чат. Владелец вносит их непосредственно в закрытые файлы на целевой машине только по инструкции следующего подтверждённого шага.

### Действия в репозитории и на VPS после отдельного разрешения

1. Добавить preview systemd unit, Nginx server block и environment example без значений секретов.
2. Подготовить ограниченный preview deploy user/script и отдельный GitHub Environment `preview`.
3. Добавить ручной preview workflow, который берёт только `dev`, повторяет Python/frontend checks, собирает static artifact и отправляет preview archive.
4. На VPS развернуть изолированный каталог, venv, базу и service; перед первым запуском выполнить backup/rollback rehearsal preview-базы.
5. Настроить TLS, проверить `/api/v1/health`, статические файлы и запрет внешнего доступа к 8002.
6. В BotFather установить HTTPS URL тестового Mini App и пройти ручной UAT из чек-листа этапа 5.

## Критерии приёмки preview

- [ ] URL открывается в Telegram по HTTPS; вне Telegram приложение не проходит auth.
- [ ] Server-side проверка подписанного `initData` работает для test bot; роли USER и ADMIN определяются backend.
- [ ] Preview не имеет доступа к production SQLite, production bot token, личному Google Calendar, production Apps Script secret или production logs.
- [ ] Пользователь и администратор проходят UAT на тестовых данных; Google Calendar details не раскрываются.
- [ ] Остановка или ошибка preview service не влияет на `calendar-bot.service` и существующий сайт.
- [ ] TLS/Nginx config проверены до reload; API слушает только loopback.
- [ ] Preview deploy запускается вручную из `dev`; production deploy из `main` не меняется.

## Точка остановки

На текущем шаге ничего из этого не создаётся. Для перехода к реализации требуется отдельное разрешение владельца на серверные изменения и внешние действия, а также готовность тестового домена, test bot и test Calendar gateway/token.
