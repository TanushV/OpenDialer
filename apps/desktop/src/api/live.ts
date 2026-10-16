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
  private stopped = true;
  private cursor = 0;
  private attempt = 0;
  private seen = new Set<string>();
  constructor(
    private options: {
      url: string;
      token: string;
      tenantId: string;
      onEvents: (events: LiveEvent[]) => void;
      onStatus: (status: string) => void;
      socketFactory?: (url: string) => SocketLike;
    },
  ) {}

  start() {
    this.stopped = false;
    this.connect();
  }

  stop() {
    this.stopped = true;
    if (this.timer) clearTimeout(this.timer);
    const socket = this.socket;
    this.socket = null;
    if (socket) {
      socket.onopen = socket.onmessage = socket.onclose = socket.onerror = null;
      socket.close();
    }
  }

  private connect() {
    if (this.stopped) return;
    this.options.onStatus("connecting");
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
        this.options.onStatus("live");
      };
      socket.onmessage = ({ data }) => {
        if (this.stopped || this.socket !== socket) return;
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
          this.options.onStatus("session_expired");
          return;
        }
        this.reconnect();
      };
      socket.onerror = () => {
        if (!this.stopped) this.options.onStatus("reconnecting");
      };
    } catch {
      this.reconnect();
    }
  }

  private reconnect() {
    if (this.stopped) return;
    this.options.onStatus("reconnecting");
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(
      () => this.connect(),
      Math.min(30000, 500 * 2 ** this.attempt++),
    );
  }
}
