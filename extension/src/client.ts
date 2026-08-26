/**
 * HTTP and SSE against the two servers: the local runtime and the gateway.
 *
 * Two things here are load-bearing and easy to get subtly wrong.
 *
 * **The SSE parser handles split frames.** A chunk boundary can land anywhere,
 * including inside a `data:` line, and a parser that assumes one chunk is one
 * frame works perfectly on a fast loopback and corrupts the first long tool
 * result over a slow link. Bytes are accumulated and only complete frames —
 * terminated by a blank line — are emitted.
 *
 * **Resumption is the point.** Every frame carries an `id:`, and a dropped
 * connection reconnects with `Last-Event-ID` / `since_id` rather than starting
 * over. Without it a dropped connection loses the live view of a run that is
 * still executing server-side, and the developer cannot tell that from the run
 * having died — which is the whole "did it die?" class of confusion.
 *
 * Consumer errors are swallowed on purpose: a disposed webview must not be able
 * to abort a stream that other surfaces are still reading.
 */

import type { ContextSnapshot, Health, QuotaSnapshot, RevertPlan, SessionSummary, WireEvent } from './protocol';
import { normaliseQuota } from './protocol';

export class HttpError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
    readonly retryAfter?: number,
  ) {
    super(detail);
    this.name = 'HttpError';
  }

  /** 429 with a server-written sentence. Never render the JSON body. */
  get isQuota(): boolean {
    return this.status === 429;
  }

  /** 410: the approval was answered, timed out, or the run ended. */
  get isGone(): boolean {
    return this.status === 410;
  }
}

export interface Fetcher {
  (input: string, init?: RequestInit): Promise<Response>;
}

/** Base for both clients: one place that turns a non-2xx into a typed error. */
abstract class Rest {
  constructor(
    protected base: string,
    protected token: () => string | undefined,
    protected fetcher: Fetcher = fetch,
  ) {}

  setBase(base: string): void {
    this.base = base.replace(/\/+$/, '');
  }

  get baseUrl(): string {
    return this.base;
  }

  protected headers(extra?: Record<string, string>): Record<string, string> {
    const token = this.token();
    return {
      'content-type': 'application/json',
      ...(token ? { authorization: `Bearer ${token}` } : {}),
      ...extra,
    };
  }

  /**
   * Called once on a 401, with the token the failing request actually sent.
   *
   * Passing the *used* token is what makes the single-flight guard work: ten
   * concurrent requests that 401 after a refresh has already landed would
   * otherwise each trigger another refresh, and each rotation invalidates the
   * last — so the storm ends with everyone signed out.
   */
  onUnauthorized?: (usedToken: string | undefined) => Promise<boolean>;

  protected async request<T>(
    method: string,
    path: string,
    body?: unknown,
    signal?: AbortSignal,
    retried = false,
  ): Promise<T> {
    const sent = this.token();
    const response = await this.fetcher(`${this.base}${path}`, {
      method,
      headers: this.headers(),
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      ...(signal ? { signal } : {}),
    });

    if (response.status === 401 && !retried && this.onUnauthorized) {
      // Retried exactly once. A second 401 after a successful refresh means the
      // account is blocked or revoked, not that the token was stale, and
      // retrying that forever turns a clean re-prompt into a hang.
      if (await this.onUnauthorized(sent)) {
        return this.request<T>(method, path, body, signal, true);
      }
    }

    if (!response.ok) {
      let detail = `${method} ${path} failed with ${response.status}`;
      try {
        const parsed = (await response.json()) as { error?: string; detail?: string };
        detail = parsed.error ?? parsed.detail ?? detail;
      } catch {
        // A non-JSON error body is still an error; the status carries the meaning.
      }
      const retry = response.headers.get('retry-after');
      throw new HttpError(response.status, detail, retry ? Number(retry) : undefined);
    }
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
  }

  protected get<T>(path: string, signal?: AbortSignal): Promise<T> {
    return this.request<T>('GET', path, undefined, signal);
  }

  protected post<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
    return this.request<T>('POST', path, body ?? {}, signal);
  }
}

// ── the local runtime ───────────────────────────────────────────────────────

export class RuntimeClient extends Rest {
  /** No token. Polled during spawn, before the extension knows anything works. */
  health(signal?: AbortSignal): Promise<Health> {
    return this.get<Health>('/v1/health', signal);
  }

  tools(): Promise<{ version: string; tools: unknown[] }> {
    return this.get('/v1/tools');
  }

  startTask(task: string, opts: { mode?: string; acceptance?: string[] } = {}): Promise<SessionSummary> {
    return this.post<SessionSummary>('/v1/tasks', { task, ...opts });
  }

  sessions(status?: string): Promise<{ sessions: SessionSummary[] }> {
    return this.get(`/v1/sessions${status ? `?status=${encodeURIComponent(status)}` : ''}`);
  }

  session(id: string, transcript = false): Promise<SessionSummary> {
    return this.get<SessionSummary>(`/v1/sessions/${id}${transcript ? '?transcript=true' : ''}`);
  }

  deleteSession(id: string): Promise<void> {
    return this.request<void>('DELETE', `/v1/sessions/${id}`);
  }

  abort(id: string): Promise<unknown> {
    return this.post(`/v1/sessions/${id}/abort`);
  }

  /** Stop after the current turn, so work in flight completes coherently. */
  windDown(id: string): Promise<unknown> {
    return this.post(`/v1/sessions/${id}/wind-down`);
  }

  /** Queue a correction the run reads before its next turn. */
  steer(id: string, text: string): Promise<{ queued: number }> {
    return this.post(`/v1/sessions/${id}/messages`, { text });
  }

  resume(id: string, note = ''): Promise<SessionSummary> {
    return this.post<SessionSummary>(`/v1/sessions/${id}/resume`, { note });
  }

  revertPlan(id: string): Promise<RevertPlan> {
    return this.get<RevertPlan>(`/v1/sessions/${id}/revert`);
  }

  revert(id: string): Promise<RevertPlan> {
    return this.post<RevertPlan>(`/v1/sessions/${id}/revert`);
  }

  context(id: string): Promise<ContextSnapshot> {
    return this.get<ContextSnapshot>(`/v1/sessions/${id}/context`);
  }

  approvals(): Promise<{ approvals: unknown[] }> {
    return this.get('/v1/approvals');
  }

  decide(
    id: string,
    decision: 'accept' | 'reject' | 'edit',
    args?: Record<string, unknown>,
  ): Promise<unknown> {
    return this.post(`/v1/approvals/${id}`, {
      decision,
      ...(args ? { arguments: args } : {}),
    });
  }

  /** Give the reviewer more time, so a slow review never becomes a rejection. */
  extendApproval(id: string): Promise<{ seconds_left: number; extensions: number }> {
    return this.post(`/v1/approvals/${id}/extend`);
  }

  /**
   * Follow a session's events, resuming from `sinceId`.
   *
   * Returns an async iterable rather than taking a callback so the consumer
   * controls back-pressure — the webview batches on `requestAnimationFrame` and
   * a push API would defeat that.
   */
  async *events(
    sessionId: string,
    sinceId: number,
    signal: AbortSignal,
  ): AsyncGenerator<WireEvent> {
    const response = await this.fetcher(
      `${this.base}/v1/sessions/${sessionId}/events?since_id=${sinceId}`,
      { headers: this.headers({ accept: 'text/event-stream' }), signal },
    );
    if (!response.ok || !response.body) {
      throw new HttpError(response.status, `the event stream refused with ${response.status}`);
    }
    yield* parseSse(response.body, signal);
  }
}

// ── the gateway ─────────────────────────────────────────────────────────────

export class GatewayClient extends Rest {
  health(): Promise<Record<string, unknown>> {
    return this.get('/v1/health');
  }

  /**
   * Step one of sign-in. The gateway issues `state`; the extension must not
   * invent it, because a state the gateway never issued cannot be checked by
   * the only party in a position to check it.
   */
  authStart(redirectUri: string, codeChallenge: string): Promise<{ state: string; authorize_url: string }> {
    return this.post('/v1/auth/start', {
      redirect_uri: redirectUri,
      code_challenge: codeChallenge,
    });
  }

  authExchange(body: {
    code: string;
    code_verifier: string;
    state: string;
    redirect_uri: string;
  }): Promise<{ access_token: string; refresh_token: string; expires_in: number; sub: string; name?: string; email?: string; roles?: string[] }> {
    return this.post('/v1/auth/exchange', body);
  }

  authRefresh(refreshToken: string): Promise<{ access_token: string; refresh_token: string; expires_in: number }> {
    return this.post('/v1/auth/refresh', { refresh_token: refreshToken });
  }

  revoke(refreshToken: string): Promise<unknown> {
    return this.post('/v1/auth/revoke', { refresh_token: refreshToken });
  }

  async quota(): Promise<QuotaSnapshot> {
    return normaliseQuota(await this.get<unknown>('/v1/quota'));
  }

  /**
   * Read quota with an explicit bearer, bypassing the stored one.
   *
   * This is how a pasted token is checked before it is written to
   * `SecretStorage`. It has to bypass `Rest.request` entirely: that path reads
   * the token from the provider's closure — which is exactly the value we do
   * not have yet — and its 401 arm would fire `onUnauthorized`, refreshing the
   * *existing* session as a side effect of validating a different credential.
   *
   * `/v1/quota` rather than `/v1/health`, because health is unauthenticated on
   * the published host: it answers 200 for a token that is complete nonsense,
   * which would make this check say yes to anything.
   */
  async quotaWith(token: string, signal?: AbortSignal): Promise<QuotaSnapshot> {
    const response = await this.fetcher(`${this.base}/v1/quota`, {
      headers: { 'content-type': 'application/json', authorization: `Bearer ${token}` },
      ...(signal ? { signal } : {}),
    });
    if (!response.ok) {
      let detail = `the gateway refused the token with ${response.status}`;
      try {
        const parsed = (await response.json()) as { error?: string; reason?: string; detail?: string };
        detail = parsed.reason ?? parsed.error ?? parsed.detail ?? detail;
      } catch {
        // A non-JSON body is still a refusal; the status carries the meaning.
      }
      throw new HttpError(response.status, detail);
    }
    return normaliseQuota(await response.json());
  }

  async preflight(estimatedTokens: number): Promise<{ ok: boolean; quota: QuotaSnapshot }> {
    const result = await this.post<{ ok?: boolean; quota?: unknown }>('/v1/quota/preflight', {
      estimated_tokens: estimatedTokens,
    });
    return { ok: result.ok === true, quota: normaliseQuota(result.quota) };
  }
}

// ── the SSE parser ──────────────────────────────────────────────────────────

/**
 * Decode an SSE byte stream into events.
 *
 * Exported for its own tests: split frames, malformed frames and the id line are
 * exactly the cases that only show up under a slow link, which is where nobody
 * is watching.
 */
export async function* parseSse(
  body: ReadableStream<Uint8Array>,
  signal?: AbortSignal,
): AsyncGenerator<WireEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let lastId = 0;
  /**
   * A lone trailing CR is held back rather than normalised.
   *
   * SSE permits CRLF, and with CRLF the frame terminator is `\r\n\r\n`, which
   * contains no `\n\n` at all — so a parser that searches for `\n\n` against a
   * CRLF server emits nothing, ever. Normalising CRLF to LF fixes that, but a
   * chunk boundary can fall *between* the CR and the LF, and normalising each
   * chunk in isolation would then leave a stray CR mid-frame. So the CR waits
   * for its LF.
   */
  let pendingCr = false;

  try {
    while (!signal?.aborted) {
      const { done, value } = await reader.read();
      if (done) break;
      let text = decoder.decode(value, { stream: true });
      if (pendingCr) {
        text = '\r' + text;
        pendingCr = false;
      }
      if (text.endsWith('\r')) {
        text = text.slice(0, -1);
        pendingCr = true;
      }
      buffer += text.replace(/\r\n/g, '\n');

      // Frames end at a blank line. Anything after the last one is a partial
      // frame and stays in the buffer until the rest of it arrives.
      let boundary = buffer.indexOf('\n\n');
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const parsed = decodeFrame(frame, lastId);
        if (parsed) {
          lastId = parsed.id;
          yield parsed;
        }
        boundary = buffer.indexOf('\n\n');
      }
    }
  } finally {
    // Cancelling a stream the consumer walked away from is not an error.
    try {
      await reader.cancel();
    } catch {
      /* already gone */
    }
  }
}

function decodeFrame(frame: string, lastId: number): WireEvent | null {
  let id = lastId;
  let type = '';
  const data: string[] = [];

  for (const raw of frame.split('\n')) {
    const line = raw.endsWith('\r') ? raw.slice(0, -1) : raw;
    // A comment. The runtime sends `: keep-alive`, which is a liveness signal
    // and not an event; EventSource would swallow it silently.
    if (line.startsWith(':') || !line) continue;
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    const value = colon === -1 ? '' : line.slice(colon + 1).replace(/^ /, '');
    if (field === 'id') id = Number(value) || lastId;
    else if (field === 'event') type = value;
    else if (field === 'data') data.push(value);
  }

  if (!type && !data.length) return null;
  let parsed: Record<string, unknown> = {};
  try {
    parsed = data.length ? (JSON.parse(data.join('\n')) as Record<string, unknown>) : {};
  } catch {
    // A malformed frame is dropped rather than thrown. One bad frame must not
    // end a run the developer is watching, and the id still advances so the
    // next reconnect does not replay it forever.
    return { id, type: type || 'error', data: { message: 'a malformed event was skipped' } };
  }
  return { id, type: type || 'message', data: parsed };
}
