'use client';

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';

import * as dateTime from '@/lib/dateTime';

import { DEFAULT_LOCALE, type Locale, LOCALE_COOKIE, resolveLocale, translate } from './core';

type LocaleContextValue = {
    formatDate: typeof dateTime.formatDate;
    formatDateTime: typeof dateTime.formatDateTime;
    formatCalendarDate: typeof dateTime.formatCalendarDate;
    formatLocalDateTime: typeof dateTime.formatLocalDateTime;
    locale: Locale;
    setLocale: (locale: Locale) => void;
    t: (message: string, values?: Record<string, string | number>) => string;
};

const LocaleContext = createContext<LocaleContextValue>({
    ...dateTime,
    locale: DEFAULT_LOCALE,
    setLocale: () => {},
    t: (message, values) => translate(DEFAULT_LOCALE, message, values),
});

export function LocaleProvider({ initialLocale = DEFAULT_LOCALE, children }: {
    initialLocale?: Locale;
    children: React.ReactNode;
}) {
    const [locale, updateLocale] = useState(initialLocale);
    const setLocale = useCallback((next: Locale) => {
        const valid = resolveLocale(next);
        updateLocale(valid);
        document.documentElement.lang = valid;
        document.cookie = `${LOCALE_COOKIE}=${valid}; Path=/; Max-Age=31536000; SameSite=Lax${location.protocol === 'https:' ? '; Secure' : ''}`;
        try { localStorage.setItem(LOCALE_COOKIE, valid); } catch { /* Cookies still persist the preference. */ }
    }, []);

    useEffect(() => {
        document.documentElement.lang = locale;
    }, [locale]);
    useEffect(() => {
        const sync = (event: StorageEvent) => {
            if (event.key === LOCALE_COOKIE) setLocale(resolveLocale(event.newValue));
        };
        window.addEventListener('storage', sync);
        return () => window.removeEventListener('storage', sync);
    }, [setLocale]);
    const value = useMemo(() => ({ locale, setLocale,
        formatDate: (value: Date | string | number, timezone?: string | null) => dateTime.formatDate(value, timezone, locale),
        formatDateTime: (value: Date | string | number, timezone?: string | null) => dateTime.formatDateTime(value, timezone, locale),
        formatCalendarDate: (value: string) => dateTime.formatCalendarDate(value, locale),
        formatLocalDateTime: (value: Date) => dateTime.formatLocalDateTime(value, locale),
        t: (message: string, values?: Record<string, string | number>) => translate(locale, message, values) }), [locale, setLocale]);
    return <LocaleContext.Provider value={value}>{children}</LocaleContext.Provider>;
}

export const useI18n = () => useContext(LocaleContext);

/** Works in server-rendered page content as well as client components. */
export function T({ text, values }: { text: string; values?: Record<string, string | number> }) {
    const { t } = useI18n();
    return <>{t(text, values)}</>;
}
