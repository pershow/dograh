import type { Client } from '@/client/client';
import type { CreateClientConfig } from '@/client/client.gen';

export function getServerBackendUrl() {
    return process.env.BACKEND_URL || 'http://api:8000';
}

/**
 * Resolve the base URL the browser should use to reach the backend API.
 *
 * Precedence:
 *   1. NEXT_PUBLIC_BACKEND_URL — explicit build-time operator config, always wins.
 *   2. backendApiEndpoint — the URL the backend reports it is running on via /health
 *      (surfaced through AppConfigContext). This is the address the browser actually
 *      reaches the backend at; for a backend on a private IP it is that private IP.
 *      Unknown at module init, so createClientConfig seeds without it and
 *      AppConfigProvider upgrades the client once /health resolves.
 *   3. window.location.origin — same-origin public deployment.
 *
 * This is the browser→API order. It is intentionally NOT tunnel-aware: the
 * Cloudflare tunnel URL is only for externally-hosted consumers (telephony
 * webhooks, MCP, external API triggers) that cannot reach a private IP — see
 * resolveWebhookBaseUrl.
 */
export function resolveBrowserBackendUrl(backendApiEndpoint?: string | null): string {
    return (
        process.env.NEXT_PUBLIC_BACKEND_URL ||
        backendApiEndpoint ||
        (typeof window !== 'undefined' ? window.location.origin : '')
    );
}

export const createClientConfig: CreateClientConfig = (config) => {
    // Use different URLs for server-side vs client-side
    const isServer = typeof window === 'undefined';
    let baseUrl: string;

    if (isServer) {
        baseUrl = getServerBackendUrl();
    } else {
        // The backend-reported endpoint is not known yet at module init;
        // AppConfigProvider upgrades the client base URL once /health reports it
        // (when no explicit NEXT_PUBLIC_BACKEND_URL is configured).
        baseUrl = resolveBrowserBackendUrl();
    }

    return {
        ...config,
        baseUrl,
    };
};

interface AuthInterceptorState {
    getAccessToken: () => Promise<string>;
    onUnauthorized?: () => void;
}

const authInterceptors = new WeakMap<Client, AuthInterceptorState>();

/**
 * Register a request interceptor that attaches a fresh access token
 * to every outgoing SDK request. Idempotent — safe for React strict mode.
 */
export function setupAuthInterceptor(
    apiClient: Client,
    getAccessToken: () => Promise<string>,
    onUnauthorized?: () => void,
) {
    const existing = authInterceptors.get(apiClient);
    if (existing) {
        // Provider remounts must not leave the interceptor reading an old token ref.
        existing.getAccessToken = getAccessToken;
        existing.onUnauthorized = onUnauthorized;
        return;
    }
    const state = { getAccessToken, onUnauthorized };
    authInterceptors.set(apiClient, state);
    const authenticatedRequests = new WeakMap<Request, string>();

    apiClient.interceptors.request.use(async (request) => {
        if (request.headers.get('Authorization')) {
            return request;
        }
        try {
            const token = await state.getAccessToken();
            if (token) {
                request.headers.set('Authorization', `Bearer ${token}`);
                authenticatedRequests.set(request, token);
            }
        } catch {
            // If token retrieval fails, let the request proceed without auth
        }
        return request;
    });

    apiClient.interceptors.response.use(async (response, request) => {
        const token = authenticatedRequests.get(request);
        if (response.status === 401 && token && state.onUnauthorized) {
            // Ignore errors from an old session or explicitly supplied credentials.
            // Permission errors (403) and service outages must not trigger sign-out.
            const currentToken = await state.getAccessToken().catch(() => '');
            if (currentToken === token) state.onUnauthorized();
        }
        return response;
    });
}
