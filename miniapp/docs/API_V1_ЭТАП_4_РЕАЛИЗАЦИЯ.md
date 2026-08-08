# API v1 — административная реализация этапа 4

Статус: локально реализовано и протестировано; deployment отсутствует.

Все маршруты ниже требуют server-side роли `ADMIN`. Изменяющие операции дополнительно требуют cookie-сессию, `X-CSRF-Token` и `Idempotency-Key`. Роль, Telegram ID, Google event ID и детали чужих событий браузер не передаёт и не получает.

| Назначение | Реализованный маршрут |
| --- | --- |
| Сводка | `GET /admin/dashboard` |
| Статистика за период до года | `GET /admin/statistics?from_date=&to_date=` |
| Безопасный статус Google Calendar | `GET /admin/integration/calendar` |
| Pending-заявки | `GET /admin/requests`, `GET /admin/requests/{id}`, `PATCH /admin/requests/{id}`, `POST /admin/requests/{id}/approve`, `POST /admin/requests/{id}/reject`, `POST /admin/requests/{id}/alternatives` |
| Запросы на перенос и отмену | `GET /admin/change-requests`, `POST /admin/change-requests/{id}/approve`, `POST /admin/change-requests/{id}/reject` |
| Ручная встреча | `GET,POST /admin/manual-meetings`, `PATCH /admin/manual-meetings/{id}`, `POST /admin/manual-meetings/{id}/cancel` |
| Повторяющиеся встречи | `GET,POST /admin/series`, `POST /admin/series/{id}/cancel`, `GET /admin/series/{id}/occurrences`, `PATCH /admin/series/{id}/occurrences/{occurrence_id}`, `POST /admin/series/{id}/occurrences/{occurrence_id}/cancel` |
| Правила и закрытые даты | `GET,PATCH /admin/settings`, `GET,POST,DELETE /admin/closed-dates` |

Безопасность данных:

- `GET /admin/integration/calendar` сообщает только `OK` или `UNAVAILABLE` и время проверки; оно не возвращает события Google Calendar.
- Статистика возвращает только агрегаты: заявки, ручные встречи, встречи календаря и число пользователей.
- При конфликте времени API сообщает нейтральный код `SLOT_UNAVAILABLE`; причина занятости и данные пересекающегося события не раскрываются.
- Для ручных встреч и серий интерфейс по умолчанию запрещает пересечение времени. Отмена, перенос отдельного повторения и обработка пользовательского запроса на изменение требуют явного подтверждения администратора в интерфейсе.

Реализация соответствует этой таблице, а расширенный [целевой контракт API v1](API_V1_КОНТРАКТ.md) сохраняет направления последующего развития и не является списком уже выпущенных маршрутов.

Границы локального этапа: проверка в настоящем Telegram, HTTPS-домен, CI/CD и production-развёртывание являются отдельными будущими этапами и не выполнялись.
