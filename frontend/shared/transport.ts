/**
 * shared/transport.ts — the GENERIC HTTP transport both RN apps build on.
 *
 * This is the "shared api client" from the parallel-agent methodology: base URL
 * + Bearer injection + JSON + uniform error semantics. It is deliberately
 * ENDPOINT-AGNOSTIC — it knows nothing about /api/panel or /api/instances. Each
 * app layers its own typed endpoint methods on top (tiles -> frontend/tiles/
 * core/panel-api.ts; admin -> its own). That keeps the shared surface small and
 * keeps each app's endpoints in its own lane (un-gated on cross-app acks).
 *
 * baseUrl: "" (same-origin) for the web build — nginx serves the bundle and
 * proxies /api/*. A native app injects an absolute origin at startup.
 */

import { authHeader } from './auth';

/** A structured transport error so callers can distinguish a real HTTP
 *  rejection (has a status) from a network/abort failure (status = 0). */
export class TransportError extends Error {
  readonly status: number;
  readonly isNetwork: boolean;
  constructor(message: string, status: number, isNetwork: boolean) {
    super(message);
    this.name = 'TransportError';
    this.status = status;
    this.isNetwork = isNetwork;
  }
}

export class Transport {
  constructor(private readonly baseUrl: string = '') {}

  private async request<T>(
    path: string,
    init: RequestInit,
  ): Promise<T> {
    let res: Response;
    try {
      res = await fetch(`${this.baseUrl}${path}`, {
        ...init,
        headers: {
          Accept: 'application/json',
          ...authHeader(),
          ...(init.headers ?? {}),
        },
      });
    } catch (e) {
      // fetch() rejected before any response -> network/abort (status 0).
      throw new TransportError(
        `network error on ${path}: ${String(e)}`,
        0,
        true,
      );
    }
    if (!res.ok) {
      throw new TransportError(`${path} -> HTTP ${res.status}`, res.status, false);
    }
    // 204/empty bodies -> undefined; callers of void endpoints ignore it.
    const text = await res.text();
    return (text ? JSON.parse(text) : undefined) as T;
  }

  get<T>(path: string): Promise<T> {
    return this.request<T>(path, { method: 'GET' });
  }

  post<T>(path: string, body?: unknown): Promise<T> {
    return this.request<T>(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  }

  put<T>(path: string, body?: unknown): Promise<T> {
    return this.request<T>(path, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  }
}
