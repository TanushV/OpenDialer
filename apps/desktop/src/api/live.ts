export interface LiveEvent {
  event_id: string;
  sequence: number;
  tenant_id: string;
  type: string;
  entity_id: string;
  entity_version: number;
  occurred_at?: string;
  payload: Record<string, unknown>;
}

interface SocketLike {
  onopen: (() => void) | null;
  onmessage: ((event: { data: string }) => void) | null;
  onclose: ((event: { code: number }) => void) | null;
  onerror: (() => void) | null;
  send: (data: string) => void;
  close: (code?: number) => void;
}

export function liveUrl(base: string): string {
  const url = new URL(base);
  const loopback = ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname);
  if (
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    (url.protocol !== "https:" && !(loopback && url.protocol === "http:"))
  )
    throw new Error("Live events require HTTPS or a loopback authority.");
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = url.pathname.replace(/\/$/, "") + "/v1/live";
  return url.toString();
}

export class LiveConnection {
  private socket: SocketLike | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private stale: ReturnType<typeof setTimeout> | null = null;
  private stopped = true;
  private cursor: number;
  private attempt = 0;
  private status = "";
  private seen = new Set<string>();
  constructor(
    private options: {
      url: string;
      token: string;
      tenantId: string;
      /** The workspace snapshot cursor; earlier events are already reflected. */
      after?: number;
      /** A socket silent this long is replaced; the authority sends no pings. */
      staleMs?: number;
      onEvents: (events: LiveEvent[]) => void;
      onStatus: (status: string) => void;
      socketFactory?: (url: string) => SocketLike;
    },
  ) {
    this.cursor = Math.max(0, Math.trunc(options.after ?? 0)) || 0;
  }

  private report(status: string) {
    if (status === this.status) return;
    this.status = status;
    this.options.onStatus(status);
  }

  private detach() {
    if (this.stale) clearTimeout(this.stale);
    this.stale = null;
    const socket = this.socket;
    this.socket = null;
    if (socket) {
      socket.onopen = socket.onmessage = socket.onclose = socket.onerror = null;
      socket.close();
    }
  }

  private watch(socket: SocketLike) {
    if (this.stale) clearTimeout(this.stale);
    this.stale = setTimeout(() => {
      if (this.stopped || this.socket !== socket) return;
      // Quietly replace a possibly half-open socket; the cursor resumes.
      this.detach();
      this.connect(true);
    }, this.options.staleMs ?? 45_000);
  }

  start() {
    this.stopped = false;
    this.connect();
  }

  stop() {
    this.stopped = true;
    if (this.timer) clearTimeout(this.timer);
    this.detach();
  }

  private connect(quiet = false) {
    if (this.stopped) return;
    if (!quiet) this.report("connecting");
    try {
      const url = liveUrl(this.options.url);
      const socket = this.options.socketFactory
        ? this.options.socketFactory(url)
        : (new WebSocket(url) as unknown as SocketLike);
      this.socket = socket;
      socket.onopen = () => {
        if (this.stopped || this.socket !== socket) return;
        socket.send(
          JSON.stringify({ token: this.options.token, after: this.cursor }),
        );
        this.attempt = 0;
        this.report("live");
        this.watch(socket);
      };
      socket.onmessage = ({ data }) => {
        if (this.stopped || this.socket !== socket) return;
        this.watch(socket);
        try {
          const batch = JSON.parse(data) as {
            events: LiveEvent[];
            cursor: number;
          };
          if (
            !Array.isArray(batch.events) ||
            !Number.isSafeInteger(batch.cursor) ||
            batch.cursor < this.cursor
          )
            return;
          const valid = batch.events.filter((event) => {
            if (
              event.tenant_id !== this.options.tenantId ||
              !event.event_id ||
              !Number.isSafeInteger(event.sequence) ||
              event.sequence <= this.cursor ||
              event.sequence > batch.cursor ||
              this.seen.has(event.event_id)
            )
              return false;
            this.seen.add(event.event_id);
            return true;
          });
          this.cursor = batch.cursor;
          if (this.seen.size > 2000)
            this.seen = new Set([...this.seen].slice(-1000));
          if (valid.length) this.options.onEvents(valid);
        } catch {
          socket.close(1003);
        }
      };
      socket.onclose = ({ code }) => {
        if (this.stopped || this.socket !== socket) return;
        if (code === 4401 || code === 4403) {
          this.stopped = true;
          this.detach();
          this.report("session_expired");
          return;
        }
        this.reconnect();
      };
      socket.onerror = () => {
        if (!this.stopped) this.report("reconnecting");
      };
    } catch {
      this.reconnect();
    }
  }

  private reconnect() {
    if (this.stopped) return;
    this.detach();
    this.report("reconnecting");
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(
      () => this.connect(),
      Math.min(30000, 500 * 2 ** this.attempt++),
    );
  }
}
