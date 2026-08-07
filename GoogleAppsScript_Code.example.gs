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
  requireText_(payload.email, 'email_required', 254);

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
    if (events.some(isBlocking_)) {
      throw new Error('slot_busy');
    }

    const options = {
      description: String(payload.description || ''),
      location: String(payload.location || ''),
      guests: String(payload.email),
      sendInvites: true,
    };
    const event = calendar.createEvent(String(payload.subject), start, end, options);
    event.setTag('telegram_request_id', String(payload.requestId));
    event.setTransparency(CalendarApp.EventTransparency.OPAQUE);
    event.setGuestsCanModify(false);
    event.setGuestsCanInviteOthers(false);
    return event.getId();
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
