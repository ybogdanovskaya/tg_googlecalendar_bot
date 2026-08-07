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
    if (payload.action === 'status') {
      return jsonResponse_(getEventStatus_(payload));
    }
    if (payload.action === 'seriesCreate') {
      return jsonResponse_({ok: true, eventId: createSeries_(payload)});
    }
    if (payload.action === 'seriesUpdate') {
      return jsonResponse_({ok: true, eventId: updateSeries_(payload)});
    }
    if (payload.action === 'seriesDelete') {
      return jsonResponse_({ok: true, deleted: deleteSeries_(payload)});
    }
    if (payload.action === 'occurrenceUpdate') {
      return jsonResponse_({ok: true, eventId: updateOccurrence_(payload)});
    }
    if (payload.action === 'occurrenceDelete') {
      return jsonResponse_({ok: true, deleted: deleteOccurrence_(payload)});
    }
    if (payload.action === 'occurrenceStatus') {
      return jsonResponse_(getOccurrenceStatus_(payload));
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

function getEventStatus_(payload) {
  requireText_(payload.eventId, 'event_id_required', 300);
  const event = CalendarApp.getDefaultCalendar().getEventById(String(payload.eventId));
  if (!event) {
    return {ok: true, exists: false};
  }
  return eventStatus_(event);
}

function eventStatus_(event) {
  return {
    ok: true,
    exists: true,
    eventId: event.getId(),
    start: event.getStartTime().toISOString(),
    end: event.getEndTime().toISOString(),
    subject: event.getTitle(),
    description: event.getDescription(),
    location: event.getLocation(),
    transparent: !isBlocking_(event),
    updated: event.getLastUpdated().toISOString(),
  };
}

function createSeries_(payload) {
  const start = parseDate_(payload.start);
  const end = parseDate_(payload.end);
  validateRange_(start, end);
  requireText_(payload.seriesId, 'series_id_required', 100);
  requireText_(payload.subject, 'subject_required', 200);
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(10000)) {
    throw new Error('calendar_busy_try_again');
  }
  try {
    const calendar = CalendarApp.getDefaultCalendar();
    const propertyKey = 'telegram_series_' + String(payload.seriesId);
    const savedId = PropertiesService.getScriptProperties().getProperty(propertyKey);
    if (savedId) {
      const saved = calendar.getEventSeriesById(savedId);
      if (saved) {
        return saved.getId();
      }
    }
    if (!payload.allowOverlap && calendar.getEvents(start, end).some(isBlocking_)) {
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
    const series = calendar.createEventSeries(
      String(payload.subject),
      start,
      end,
      buildRecurrence_(payload),
      options,
    );
    series.setTag('telegram_series_id', String(payload.seriesId));
    series.setTransparency(payload.transparent
      ? CalendarApp.EventTransparency.TRANSPARENT
      : CalendarApp.EventTransparency.OPAQUE);
    series.setGuestsCanModify(false);
    series.setGuestsCanInviteOthers(false);
    PropertiesService.getScriptProperties().setProperty(propertyKey, series.getId());
    return series.getId();
  } finally {
    lock.releaseLock();
  }
}

function updateSeries_(payload) {
  requireText_(payload.eventId, 'event_id_required', 300);
  requireText_(payload.subject, 'subject_required', 200);
  const start = parseDate_(payload.start);
  const end = parseDate_(payload.end);
  validateRange_(start, end);
  const series = CalendarApp.getDefaultCalendar().getEventSeriesById(String(payload.eventId));
  if (!series) {
    throw new Error('series_not_found');
  }
  series.setTitle(String(payload.subject));
  series.setDescription(String(payload.description || ''));
  series.setLocation(String(payload.location || ''));
  series.setTransparency(payload.transparent
    ? CalendarApp.EventTransparency.TRANSPARENT
    : CalendarApp.EventTransparency.OPAQUE);
  series.setRecurrence(buildRecurrence_(payload), start, end);
  return series.getId();
}

function deleteSeries_(payload) {
  requireText_(payload.eventId, 'event_id_required', 300);
  const series = CalendarApp.getDefaultCalendar().getEventSeriesById(String(payload.eventId));
  if (!series) {
    return false;
  }
  series.deleteEventSeries();
  return true;
}

function updateOccurrence_(payload) {
  const event = findOccurrence_(payload);
  if (!event) {
    throw new Error('occurrence_not_found');
  }
  const start = parseDate_(payload.start);
  const end = parseDate_(payload.end);
  validateRange_(start, end);
  event.setTime(start, end);
  return event.getId();
}

function deleteOccurrence_(payload) {
  const event = findOccurrence_(payload);
  if (!event) {
    return false;
  }
  event.deleteEvent();
  return true;
}

function getOccurrenceStatus_(payload) {
  const event = findOccurrenceForStatus_(payload);
  if (!event) {
    return {ok: true, exists: false};
  }
  return eventStatus_(event);
}

function findOccurrenceForStatus_(payload) {
  const current = findOccurrence_(payload);
  if (current) {
    return current;
  }
  requireText_(payload.eventId, 'event_id_required', 300);
  const expected = parseDate_(payload.expectedStart);
  const margin = 31 * 24 * 60 * 60 * 1000;
  const originalMargin = 2 * 60 * 1000;
  return CalendarApp.getDefaultCalendar()
    .getEvents(new Date(expected.getTime() - margin), new Date(expected.getTime() + margin))
    .find(function(event) {
      if (event.getId() !== String(payload.eventId)) {
        return false;
      }
      const original = event.getOriginalStartTime();
      return original && Math.abs(original.getTime() - expected.getTime()) < originalMargin;
    }) || null;
}

function findOccurrence_(payload) {
  requireText_(payload.eventId, 'event_id_required', 300);
  const lookup = parseDate_(payload.lookupStart);
  const margin = 2 * 60 * 1000;
  return CalendarApp.getDefaultCalendar()
    .getEvents(new Date(lookup.getTime() - margin), new Date(lookup.getTime() + margin))
    .find(function(event) {
      return event.getId() === String(payload.eventId)
        && Math.abs(event.getStartTime().getTime() - lookup.getTime()) < margin;
    }) || null;
}

function buildRecurrence_(payload) {
  const recurrence = CalendarApp.newRecurrence().setTimeZone('Europe/Moscow');
  const frequency = String(payload.frequency || '');
  let rule;
  if (frequency === 'DAILY') {
    rule = recurrence.addDailyRule();
  } else if (frequency === 'WEEKLY') {
    rule = recurrence.addWeeklyRule();
  } else if (frequency === 'MONTHLY') {
    rule = recurrence.addMonthlyRule();
  } else {
    throw new Error('invalid_frequency');
  }
  const until = new Date(String(payload.untilDate || '') + 'T23:59:59+03:00');
  if (isNaN(until.getTime())) {
    throw new Error('invalid_until_date');
  }
  rule.until(until);
  return recurrence;
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
