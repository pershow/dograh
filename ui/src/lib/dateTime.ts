import { DEFAULT_LOCALE, type Locale } from './i18n/core';

export function getLocalTimezone() {
    return Intl.DateTimeFormat().resolvedOptions().timeZone;
}

type DateInput = Date | number | string;

export function formatDateTime(value: DateInput, timezone?: string | null, locale: Locale = DEFAULT_LOCALE) {
    return new Date(value).toLocaleString(locale === "en" ? "en-US" : locale, {
        timeZone: timezone || getLocalTimezone(),
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        hour12: locale === "en",
    });
}

export function formatDate(value: DateInput, timezone?: string | null, locale: Locale = DEFAULT_LOCALE) {
    return new Date(value).toLocaleDateString(locale === "en" ? "en-US" : locale, {
        timeZone: timezone || getLocalTimezone(),
        year: 'numeric',
        month: 'short',
        day: 'numeric',
    });
}

export function formatCalendarDate(value: string, locale: Locale = DEFAULT_LOCALE) {
    const calendarDate = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
    if (!calendarDate) {
        return formatDate(value, 'UTC', locale);
    }

    const [, year, month, day] = calendarDate;
    return formatDate(
        Date.UTC(Number(year), Number(month) - 1, Number(day)),
        'UTC',
        locale,
    );
}

export function formatLocalDateTime(value: Date, locale: Locale = DEFAULT_LOCALE) {
    return `${value.toLocaleDateString(locale)} ${value.toLocaleTimeString(locale, {
        hour: '2-digit',
        minute: '2-digit',
    })}`;
}
