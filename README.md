# Telegram Calendar Bot — ультрапилот

Лёгкий Telegram-бот для запроса и согласования встреч в одном Google Calendar.

## Архитектура

- Python 3.12;
- aiogram 3, Telegram long polling;
- Google Calendar API по OAuth 2.0;
- SQLite в WAL-режиме;
- systemd для запуска и автоперезапуска;
- JSONL-логи и отдельный аудит в базе.

## Необходимые секреты

Секреты не хранятся в Git. Для запуска нужны:

- Telegram Bot Token;
- Telegram ID администратора;
- OAuth Desktop Client JSON из Google Cloud;
- полученный после разрешения Google token.

## Локальная проверка

```powershell
python -m unittest discover -s tests -v
python -m compileall app scripts tests
```

## Запуск

После задания переменных и получения Google token:

```bash
python -m app.main
```

Производственный запуск выполняется через файлы из `deploy/`.
