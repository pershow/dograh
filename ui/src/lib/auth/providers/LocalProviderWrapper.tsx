'use client';

import React, { useEffect, useMemo, useRef, useState } from 'react';

import { Button } from '@/components/ui/button';
import logger from '@/lib/logger';

import type { AuthUser, LocalUser } from '../types';
import { AuthContext } from './AuthProvider';

export function LocalProviderWrapper({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<LocalUser | null>(null);
  const [loading, setLoading] = useState(true);
  const [authError, setAuthError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const tokenRef = useRef<string | null>(null);
  const redirecting = useRef(false);

  const redirectToLogin = React.useCallback(() => {
    if (redirecting.current) return;
    redirecting.current = true;
    tokenRef.current = null;
    setUser(null);
    window.location.href = '/auth/login';
  }, []);

  useEffect(() => {
    if (typeof window === 'undefined') return;
    const controller = new AbortController();
    setLoading(true);
    setAuthError(false);

    const initializeAuth = async () => {
      try {
        const response = await fetch('/api/auth/oss', {
          cache: 'no-store',
          signal: controller.signal,
        });
        if (controller.signal.aborted) return;
        if (response.ok) {
          const data = await response.json();
          if (controller.signal.aborted) return;
          tokenRef.current = data.token;
          setUser(data.user);
          logger.info('OSS auth initialized', { user: data.user });
        } else if (response.status === 401) {
          tokenRef.current = null;
          setUser(null);
          if (!window.location.pathname.startsWith('/auth/')) {
            redirectToLogin();
            return;
          }
        } else {
          setAuthError(true);
          logger.error('Failed to initialize OSS auth');
        }
      } catch (error) {
        if (controller.signal.aborted) return;
        setAuthError(true);
        logger.error('Error initializing OSS auth', error);
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    };

    initializeAuth();
    return () => controller.abort();
  }, [attempt, redirectToLogin]);

  const getAccessToken = React.useCallback(async () => {
    if (typeof window === 'undefined') {
      return 'ssr-placeholder-token';
    }
    if (!tokenRef.current) {
      logger.warn('No OSS token available after initialization');
      return '';
    }
    return tokenRef.current;
  }, []);

  const logout = React.useCallback(async () => {
    try {
      await fetch('/api/auth/logout', { method: 'POST' });
    } catch (error) {
      logger.error('Error during logout', error);
    }
    setUser(null);
    tokenRef.current = null;
    window.location.href = '/auth/login';
  }, []);

  const contextValue = useMemo(() => ({
    user: user as AuthUser,
    isAuthenticated: !!user,
    loading,
    getAccessToken,
    redirectToLogin,
    logout,
    provider: 'local' as const,
  }), [user, loading, getAccessToken, redirectToLogin, logout]);

  return (
    <AuthContext.Provider value={contextValue}>
      {authError && !window.location.pathname.startsWith('/auth/') ? (
        <div role="alert" className="flex min-h-screen flex-col items-center justify-center gap-4 p-6">
          <p>Unable to verify your session. Please try again.</p>
          <Button onClick={() => setAttempt(value => value + 1)}>Retry</Button>
        </div>
      ) : children}
    </AuthContext.Provider>
  );
}
