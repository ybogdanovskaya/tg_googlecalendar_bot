# Контракт HTTP API v1

Статус: целевой контракт v1, 8 августа 2026 года. На этапе 2 реализована и покрыта тестами безопасная основа; полный набор маршрутов будет закрываться по этапам ниже.

## Статус реализации

На локальных этапах 2–3 реализованы: Telegram auth с серверной проверкой `initData`, короткая cookie-сессия, CSRF, `GET /me`, согласие и политика, конфигурация бронирования, календарные даты и слоты без раскрытия содержимого Google Calendar, а также полный пользовательский цикл заявки: список, просмотр, создание, изменение, отмена, альтернативы, запрос переноса/отмены назначенной встречи и подтверждаемое удаление данных. Создание защищено серверной идемпотентностью.

Административные маршруты будут реализованы и протестированы вместе с административным интерфейсом на этапе 4. До этого таблицы ниже описывают целевой административный, а не уже доступный UI-контракт.

## 1. Общие правила

- Base URL production: `https://calendar.yibogdanovskaya.ru/api/v1`; локально — тот же path за локальным reverse proxy.
- JSON UTF-8, даты-время — ISO 8601 с UTC offset; часовой пояс продукта `Europe/Moscow` возвращается в bootstrap-конфигурации.
- Клиент не передаёт роль, Telegram ID, срок резерва, статус, вычисленные слоты или признак пересечения как доверенные значения.
- Списки используют `limit` (1–100) и непрозрачный `cursor`; API не принимает SQL-подобные фильтры.
- Внешние Google ID, данные занятых событий, имена/почты иных пользователей не выдаются пользовательским endpoint'ам.

## 2. Авторизация

### `POST /auth/telegram`

Тело: `{ "init_data": "<raw Telegram.WebApp.initData>" }`.

Backend валидирует HMAC и `auth_date`, определяет пользователя и роль, создаёт серверную сессию. Ответ устанавливает `__Host-calendar_session` (`Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`) и возвращает:

```json
{
  "user": { "display_name": "…", "role": "USER", "consent": { "accepted": true, "version": "2026-08-07" } },
  "csrf_token": "…",
  "expires_at": "2026-08-08T12:30:00+03:00"
}
```

`csrf_token` передаётся в `X-CSRF-Token` для всех изменяющих запросов. `POST`, `PATCH`, `PUT` и `DELETE` также требуют `Idempotency-Key` — новый UUID для логического действия. `DELETE /auth/session` завершает сессию.

### `GET /me`

Возвращает роль, consent, разрешённые возможности, часовой пояс и срок сессии. Это единственный источник UI-прав.

## 3. Общий формат ошибок

```json
{
  "error": {
    "code": "SLOT_UNAVAILABLE",
    "message": "Это время уже занято. Выберите другой слот.",
    "retryable": false,
    "request_id": "opaque-id",
    "field_errors": { "start_at": "…" }
  }
}
```

| HTTP | Код | Значение для UI |
| --- | --- | --- |
| 401 | `AUTH_REQUIRED`, `AUTH_INVALID` | Повторить Telegram authorization |
| 403 | `ACCESS_DENIED`, `CONSENT_REQUIRED`, `CSRF_INVALID` | Не показывать объект; открыть policy при необходимости |
| 404 | `NOT_FOUND` | Объект не найден или недоступен пользователю |
| 409 | `SLOT_UNAVAILABLE`, `CONFLICT`, `IDEMPOTENCY_CONFLICT` | Обновить данные/слоты, не повторять автоматически |
| 422 | `VALIDATION_ERROR` | Подсветить поля |
| 429 | `RATE_LIMITED` | Показать время безопасного повтора |
| 503 | `EXTERNAL_UNAVAILABLE` | Предложить повторить позднее |
| 500 | `INTERNAL_ERROR` | Нейтральное сообщение с `request_id` |

## 4. Пользовательские endpoint'ы

| Метод и путь | Назначение | Авторизация |
| --- | --- | --- |
| `GET /booking/config` | Длительности, горизонт, окно, шаг, policy version | Сессия |
| `GET /booking/calendar?from=&to=` | Допустимые и закрытые даты без сведений о календаре | Сессия |
| `GET /booking/slots?date=YYYY-MM-DD&duration_minutes=` | Разрешённые слоты и количество по частям дня | Сессия |
| `POST /requests` | Создать заявку и резерв | Consent + idempotency |
| `GET /requests` | Список только своих заявок | Сессия |
| `GET /requests/{request_id}` | Карточка только своей заявки | Ownership |
| `PATCH /requests/{request_id}` | Изменить pending-заявку | Ownership + idempotency |
| `POST /requests/{request_id}/cancel` | Отменить pending-заявку | Ownership + idempotency |
| `GET /requests/{request_id}/alternatives` | Варианты администратора | Ownership |
| `POST /requests/{request_id}/alternatives/{alternative_id}/accept` | Выбрать альтернативу | Ownership + idempotency |
| `POST /requests/{request_id}/alternatives/decline` | Отклонить варианты | Ownership + idempotency |
| `POST /requests/{request_id}/change-requests` | Запросить перенос или отмену назначенной встречи | Ownership + idempotency |
| `GET /privacy-policy` | Актуальная политика без персональных данных | Сессия |
| `POST /consents` | Принять актуальную версию | Idempotency |
| `POST /deletion-requests` | Создать подтверждаемый запрос удаления | Ownership + idempotency |
| `POST /deletion-requests/{id}/confirm` | Подтвердить свой запрос | Ownership + idempotency |

`POST /requests` принимает только: имя, email, тему, необязательные описание/место, `start_at` и длительность. Backend вычисляет окончание, проверяет согласие, правила, локальные резервы и Google availability в транзакционно безопасном порядке.

## 5. Административные endpoint'ы

Все пути ниже требуют server-side роли `ADMIN`, CSRF и idempotency для mutation.

| Группа | Пути |
| --- | --- |
| Dashboard и заявки | `GET /admin/dashboard`, `GET /admin/requests`, `GET /admin/requests/{id}`, `PATCH /admin/requests/{id}`, `POST /admin/requests/{id}/approve`, `POST /admin/requests/{id}/reject`, `POST /admin/requests/{id}/alternatives`, `DELETE /admin/requests/{id}` |
| Разовые события | `GET,POST /admin/events`, `GET,PATCH,DELETE /admin/events/{id}`, `POST /admin/events/{id}/confirm-overlap` |
| Серии | `GET,POST /admin/series`, `GET,PATCH,DELETE /admin/series/{id}`, `GET /admin/series/{id}/occurrences`, `PATCH /admin/series/{id}/occurrences/{occurrence_id}`, `DELETE /admin/series/{id}/occurrences/{occurrence_id}` |
| Настройки | `GET /admin/settings`, `PATCH /admin/settings`, `GET,POST,DELETE /admin/closed-dates`, `GET,PATCH /admin/reminders`, `GET,PATCH /admin/privacy-policy` |
| Контроль | `GET /admin/statistics?from=&to=`, `GET /admin/integration/calendar`, `GET /admin/sync/issues`, `POST /admin/sync/issues/{id}/retry` |

Для ручной встречи или серии API принимает флаг блокировки и при конфликте возвращает `409 CONFLICT` с нейтральным `confirmation_token`, действующим ограниченное время. Только последующий endpoint подтверждения с тем же payload и токеном создаёт пересечение. Содержимое конфликтующего Google-события не возвращается.

## 6. Представления данных

### Meeting request

```json
{
  "id": "123",
  "subject": "Консультация",
  "start_at": "2026-08-12T10:00:00+03:00",
  "end_at": "2026-08-12T10:30:00+03:00",
  "duration_minutes": 30,
  "status": "PENDING",
  "status_label": "На согласовании",
  "reservation": { "active": true, "until": "2026-08-13T10:00:00+03:00" },
  "allowed_actions": ["EDIT", "CANCEL"],
  "created_at": "2026-08-08T10:00:00+03:00",
  "updated_at": "2026-08-08T10:00:00+03:00"
}
```

Пользовательское представление не включает `telegram_id`, Google ID, поля иных заявителей или технический sync state. Административное представление добавляет только данные заявителя, необходимые для обработки, и audit metadata без Google event details.

### Settings и статистика

Настройки возвращаются именованными, валидированными значениями и метаданными `updated_at`, `updated_by_role`; история изменений запрашивается отдельно. Статистика содержит агрегаты по запрошенному периоду, не сырой экспорт персональных данных.

## 7. Правила согласованности

1. Frontend не считает доступность: backend является единственным источником истины.
2. Каждый mutation имеет `Idempotency-Key`; backend сохраняет безопасный результат ключа и запрещает тот же ключ с иным payload.
3. Создание, изменение, принятие альтернативы и админ-согласование повторно проверяют слот в транзакции SQLite `BEGIN IMMEDIATE`; Google проверяется перед операцией создания события.
4. При race condition клиент получает `SLOT_UNAVAILABLE`, а не сведения о конфликте.
5. На создании Google event применяются существующие доменные правила и проверка результата перед повторной попыткой.
6. API не доверяет `role`, `telegram_id`, `status`, `hold_until`, `blocks_calendar` или `allow_overlap`, переданным frontend без server-side проверки.

## 8. OpenAPI и contract tests

До реализации этот документ преобразуется в versioned OpenAPI 3.1 schema. Минимальные contract tests покрывают auth, ownership/IDOR, consent, validation, idempotency, истёкший резерв, race condition слотов, безопасные ошибки Google и все ADMIN endpoints. Интерактивная документация отключается в production; schema остаётся версионированным артефактом CI.
