// Безопасный шаблон. Перед размещением в Google Apps Script замените значение
// SHARED_SECRET случайной строкой и сохраните ту же строку на сервере бота.
const SHARED_SECRET = 'REPLACE_WITH_RANDOM_SHARED_SECRET';
const MAX_RANGE_DAYS = 31;

function doGet() {
  return jsonResponse_({ok: true, service: 'telegram-calendar-bridge'});
}

function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents) {
      throw new Error('empty_request');
    }
    const payload = JSON.parse(e.postData.contents);
    if (payload.secret !== SHARED_SECRET) {
      throw new Error('unauthorized');
    }
    if (payload.action === 'busy') {
      return jsonResponse_({ok: true, busy: getBusy_(payload)});
    }
    if (payload.action === 'create') {
      return jsonResponse_({ok: true, eventId: createEvent_(payload)});
    }
    if (payload.action === 'update') {
      return jsonResponse_({ok: true, eventId: updateEvent_(payload)});
    }
    if (payload.action === 'delete') {
      return jsonResponse_({ok: true, deleted: deleteEvent_(payload)});
    }
    throw new Error('unknown_action');
  } catch (error) {
    return jsonResponse_({
      ok: false,
      error: String(error && error.message ? error.message : error),
    });
  }
}

function getBusy_(payload) {
  const start = parseDate_(payload.start);
  const end = parseDate_(payload.end);
  validateRange_(start, end);
  const calendar = CalendarApp.getDefaultCalendar();
  return calendar
    .getEvents(start, end)
    .filter(isBlocking_)
    .map(function(event) {
      return {
        start: event.getStartTime().toISOString(),
        end: event.getEndTime().toISOString(),
      };
    });
}

function createEvent_(payload) {
  const start = parseDate_(payload.start);
  const end = parseDate_(payload.end);
  validateRange_(start, end);
  requireText_(payload.requestId, 'request_id_required', 100);
  requireText_(payload.subject, 'subject_required', 200);

  const lock = LockService.getScriptLock();
  if (!lock.tryLock(10000)) {
    throw new Error('calendar_busy_try_again');
  }

  try {
    const calendar = CalendarApp.getDefaultCalendar();
    const events = calendar.getEvents(start, end);
    const existing = events.find(function(event) {
      return event.getTag('telegram_request_id') === String(payload.requestId);
    });
    if (existing) {
      return existing.getId();
    }
    if (!payload.allowOverlap && events.some(isBlocking_)) {
      throw new Error('slot_busy');
    }

    const options = {
      description: String(payload.description || ''),
      location: String(payload.location || ''),
    };
    if (String(payload.email || '').trim()) {
      options.guests = String(payload.email).trim();
      options.sendInvites = true;
    }
    const event = calendar.createEvent(String(payload.subject), start, end, options);
    event.setTag('telegram_request_id', String(payload.requestId));
    event.setTransparency(payload.transparent
      ? CalendarApp.EventTransparency.TRANSPARENT
      : CalendarApp.EventTransparency.OPAQUE);
    event.setGuestsCanModify(false);
    event.setGuestsCanInviteOthers(false);
    return event.getId();
  } finally {
    lock.releaseLock();
  }
}

function updateEvent_(payload) {
  const start = parseDate_(payload.start);
  const end = parseDate_(payload.end);
  validateRange_(start, end);
  requireText_(payload.eventId, 'event_id_required', 300);
  requireText_(payload.subject, 'subject_required', 200);
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(10000)) {
    throw new Error('calendar_busy_try_again');
  }
  try {
    const calendar = CalendarApp.getDefaultCalendar();
    const event = calendar.getEventById(String(payload.eventId));
    if (!event) {
      throw new Error('event_not_found');
    }
    const conflicts = calendar.getEvents(start, end).filter(function(item) {
      return item.getId() !== event.getId() && isBlocking_(item);
    });
    if (!payload.allowOverlap && conflicts.length) {
      throw new Error('slot_busy');
    }
    event.setTitle(String(payload.subject));
    event.setTime(start, end);
    event.setDescription(String(payload.description || ''));
    event.setLocation(String(payload.location || ''));
    event.setTransparency(payload.transparent
      ? CalendarApp.EventTransparency.TRANSPARENT
      : CalendarApp.EventTransparency.OPAQUE);
    return event.getId();
  } finally {
    lock.releaseLock();
  }
}

function deleteEvent_(payload) {
  requireText_(payload.eventId, 'event_id_required', 300);
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(10000)) {
    throw new Error('calendar_busy_try_again');
  }
  try {
    const event = CalendarApp.getDefaultCalendar().getEventById(String(payload.eventId));
    if (!event) {
      return false;
    }
    event.deleteEvent();
    return true;
  } finally {
    lock.releaseLock();
  }
}

function isBlocking_(event) {
  return event.getTransparency() !== CalendarApp.EventTransparency.TRANSPARENT;
}

function parseDate_(value) {
  const date = new Date(String(value || ''));
  if (isNaN(date.getTime())) {
    throw new Error('invalid_date');
  }
  return date;
}

function validateRange_(start, end) {
  if (end <= start) {
    throw new Error('invalid_range');
  }
  if (end.getTime() - start.getTime() > MAX_RANGE_DAYS * 24 * 60 * 60 * 1000) {
    throw new Error('range_too_large');
  }
}

function requireText_(value, errorCode, maxLength) {
  const text = String(value || '').trim();
  if (!text || text.length > maxLength) {
    throw new Error(errorCode);
  }
}

function jsonResponse_(payload) {
  return ContentService
    .createTextOutput(JSON.stringify(payload))
    .setMimeType(ContentService.MimeType.JSON);
}
