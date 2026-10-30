import { useCallback, useEffect, useState } from "react";
import type { SetStateAction } from "react";

/** Keep an unsent attachment selection through a reload; scope it to this chat. */
export function useAttachmentSelection(session: string) {
  const key = `workbench:attachments:${session}`;
  const [selected, setSelected] = useState<string[]>([]);
  useEffect(() => {
    try {
      const value: unknown = session ? JSON.parse(sessionStorage.getItem(key) || "[]") : [];
      setSelected(Array.isArray(value) ? value.filter((id): id is string => typeof id === "string").slice(0, 5) : []);
    } catch { setSelected([]); }
  }, [key, session]);
  const update = useCallback((action: SetStateAction<string[]>) => {
    setSelected(previous => {
      const next = typeof action === "function" ? action(previous) : action;
      try { if (session) sessionStorage.setItem(key, JSON.stringify(next)); } catch { /* Storage may be disabled. */ }
      return next;
    });
  }, [key, session]);
  return [selected, update] as const;
}
