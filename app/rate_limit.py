from __future__ import annotations

import logging
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject


LOGGER = logging.getLogger(__name__)


class UserRateLimitMiddleware(BaseMiddleware):
    def __init__(self, admin_id: int, window_seconds: float = 10.0, user_limit: int = 30, admin_limit: int = 100) -> None:
        self.admin_id = admin_id
        self.window_seconds = window_seconds
        self.user_limit = user_limit
        self.admin_limit = admin_limit
        self._events: dict[int, deque[float]] = defaultdict(deque)
        self._last_warning: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)
        now = monotonic()
        history = self._events[user.id]
        while history and history[0] <= now - self.window_seconds:
            history.popleft()
        limit = self.admin_limit if user.id == self.admin_id else self.user_limit
        if len(history) >= limit:
            if now - self._last_warning.get(user.id, 0.0) >= self.window_seconds:
                self._last_warning[user.id] = now
                LOGGER.warning("telegram_rate_limited", extra={"telegram_id": user.id, "limit": limit})
                if isinstance(event, CallbackQuery):
                    await event.answer("Слишком много действий. Подождите несколько секунд.", show_alert=True)
                elif isinstance(event, Message):
                    await event.answer("Слишком много сообщений. Подождите несколько секунд.")
            return None
        history.append(now)
        if len(self._events) > 1000:
            stale = [key for key, values in self._events.items() if not values or values[-1] <= now - self.window_seconds]
            for key in stale:
                self._events.pop(key, None)
                self._last_warning.pop(key, None)
        return await handler(event, data)
