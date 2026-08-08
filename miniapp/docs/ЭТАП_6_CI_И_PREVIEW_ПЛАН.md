# Этап 6 — CI и изолированный HTTPS preview

Статус: подготовка. Production, `main`, VPS и GitHub Environment не менялись.

## Что обнаружено

Текущий workflow [CI](../../.github/workflows/ci.yml) запускает Python-проверки для `dev`, `main` и feature-веток. Он не запускает `npm test` и `npm run build` внутри `miniapp/`.

Текущий production workflow [Deploy production](../../.github/workflows/deploy-production.yml) запускается только вручную из `main`, требует GitHub Environment `production` и архивирует только существующую Python-часть. Mini App в него не включён. Это верное безопасное состояние до отдельного плана выпуска Mini App.

## Предлагаемая подзадача 6.1 — frontend CI

Изменяется только `.github/workflows/ci.yml`, потому что GitHub Actions читает workflow именно из этой папки; разместить его внутри `miniapp/` технически невозможно.

Добавляется независимая задача `miniapp-checks`:

1. запускается на GitHub-hosted Ubuntu runner;
2. использует Node.js LTS только во время CI;
3. выполняет `npm ci` в `miniapp/` по committed `package-lock.json`;
4. выполняет `npm test`;
5. выполняет `npm run build`;
6. проверяет, что `dist/` создан и не добавлен в Git;
7. не имеет secrets, SSH-доступа, доступа к VPS, Google Calendar или production-данным.

Результат: любой commit в `dev`, `feature/*` или `main` не пройдёт CI при поломке TypeScript, тестов или статической сборки Mini App.

## Предлагаемая подзадача 6.2 — preview-проектирование

Настоящий запуск внутри Telegram требует публичного HTTPS URL. Локальный `127.0.0.1` не может заменить этот URL, так как Telegram должен передать подписанный `initData` реальному Mini App.

Для будущего preview нужны отдельные, изолированные объекты:

- HTTPS-поддомен или временный preview URL;
- отдельный тестовый Telegram-бот с настроенной кнопкой Mini App;
- отдельные тестовые секреты и тестовый календарь/шлюз, не владельческий production Calendar;
- отдельная тестовая SQLite-база;
- отдельная ASGI-служба и Nginx location, не меняющие long polling бота и существующий сайт.

Создание такого preview затрагивает DNS, Telegram BotFather, VPS, Nginx, systemd и конфигурацию вне `miniapp/`. Оно не входит в подзадачу 6.1 и потребует отдельного технического плана и подтверждения владельца.

## GitHub Environment и production

Открытый вопрос из `PROJECT_CONTEXT.md` остаётся без изменений: владелец должен авторизоваться в GitHub, создать Environment `production`, добавить ограниченные deploy secrets, назначить reviewer и включить защиту `main`. До этого production CD и выпуск Mini App не выполняются.

## Критерии приёмки подзадачи 6.1

- [x] В workflow добавлена независимая задача Mini App checks; её запуск в GitHub Actions проверяется после отправки commit в `dev`.
- [x] Mini App checks используют lockfile и не публикуют secrets.
- [ ] `npm test` и `npm run build` проходят на чистом GitHub-hosted runner.
- [ ] Сбой frontend-проверки делает CI красным, но не запускает deploy.
- [x] Ничего не передаётся на VPS, GitHub Environment и `main` не меняются.

## Что нужно подтвердить владельцу

Разрешение изменить один файл вне `miniapp/`: `.github/workflows/ci.yml`, чтобы добавить только описанную выше CI-задачу. После этого можно будет проверить результат GitHub Actions в `dev`; внешняя авторизация для неё не требуется.
