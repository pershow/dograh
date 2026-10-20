"use client";

import { useEffect, useState } from "react";

import { workbenchCapabilitiesApiV1AgentWorkbenchCapabilitiesGet } from "@/client/sdk.gen";
import { useAuth } from "@/lib/auth";

/** Read the deployment capability only after the SDK has its authenticated token. */
export function useWorkbenchEnabled() {
  const { user, loading } = useAuth();
  const [enabled, setEnabled] = useState(false);
  useEffect(() => {
    if (loading || !user) return;
    let disposed = false;
    workbenchCapabilitiesApiV1AgentWorkbenchCapabilitiesGet()
      .then(response => {
        if (!disposed) setEnabled(!response.error && response.data?.data.enabled === true);
      })
      .catch(() => { if (!disposed) setEnabled(false); });
    return () => { disposed = true; };
  }, [loading, user]);
  return !loading && !!user && enabled;
}
