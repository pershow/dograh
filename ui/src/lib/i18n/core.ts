import messages from './messages.json';

export type Locale = 'zh-CN' | 'en';
export const DEFAULT_LOCALE: Locale = 'zh-CN';
export const LOCALE_COOKIE = 'dograh-locale';
export function resolveLocale(value?: string | null): Locale {
    return value === 'en' ? 'en' : DEFAULT_LOCALE;
}

// Source messages are stable keys; values and API identifiers are never translated.
export function translate(locale: Locale, message: string, values?: Record<string, string | number>): string {
    const pair = (messages as Record<string, string[]>)[message];
    const translated = pair ? pair[locale === 'zh-CN' ? 0 : 1] : message;
    return values ? translated.replace(/\{(\w+)\}/g, (match, key) => String(values[key] ?? match)) : translated;
}
