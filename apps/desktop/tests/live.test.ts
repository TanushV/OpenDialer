import { afterEach, describe, expect, it, vi } from "vitest";
import { LiveConnection, liveUrl } from "../src/api/live";

class Socket {
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  sent: string[] = [];
  close = vi.fn();
  send(data: string) {
    this.sent.push(data);
  }
}

afterEach(() => vi.useRealTimers());

describe("authenticated live events", () => {
  it("uses secure websocket URLs and never puts credentials in the URL", () => {
    expect(liveUrl("https://dialer.example.test")).toBe(
      "wss://dialer.example.test/v1/live",
    );
    expect(liveUrl("http://127.0.0.1:1234")).toBe(
      "ws://127.0.0.1:1234/v1/live",
    );
    expect(() => liveUrl("http://remote.example.test")).toThrow();
    expect(() =>
      liveUrl("https://user:password@remote.example.test"),
    ).toThrow();
  });

  it("authenticates in the first frame, deduplicates and replays from its cursor", () => {
    vi.useFakeTimers();
    const sockets: Socket[] = [];
    const received = vi.fn();
    const live = new LiveConnection({
      url: "https://dialer.example.test",
      token: "protected-token",
      tenantId: "t",
      onEvents: received,
      onStatus: vi.fn(),
      socketFactory: () => {
        const s = new Socket();
        sockets.push(s);
        return s;
      },
    });
    live.start();
    sockets[0].onopen?.();
    expect(JSON.parse(sockets[0].sent[0])).toEqual({
      token: "protected-token",
      after: 0,
    });
    const event = {
      event_id: "e",
      sequence: 7,
      tenant_id: "t",
      type: "coach.card_ready",
      entity_id: "c",
      entity_version: 2,
      payload: {},
    };
    sockets[0].onmessage?.({
      data: JSON.stringify({ cursor: 7, events: [event, event] }),
    });
    expect(received).toHaveBeenCalledTimes(1);
    expect(received.mock.calls[0][0]).toHaveLength(1);
    sockets[0].onclose?.({ code: 1006 });
    vi.advanceTimersByTime(30000);
    sockets[1].onopen?.();
    expect(JSON.parse(sockets[1].sent[0]).after).toBe(7);
    live.stop();
    expect(sockets[1].close).toHaveBeenCalled();
  });

  it("rejects foreign tenants and stops reconnecting after authentication expires", () => {
    vi.useFakeTimers();
    const socket = new Socket();
    const received = vi.fn(),
      status = vi.fn(),
      factory = vi.fn(() => socket);
    const live = new LiveConnection({
      url: "https://dialer.example.test",
      token: "test",
      tenantId: "own",
      onEvents: received,
      onStatus: status,
      socketFactory: factory,
    });
    live.start();
    socket.onmessage?.({
      data: JSON.stringify({
        cursor: 1,
        events: [
          {
            event_id: "foreign",
            sequence: 1,
            tenant_id: "other",
            type: "call.updated",
          },
        ],
      }),
    });
    expect(received).not.toHaveBeenCalled();
    socket.onclose?.({ code: 4401 });
    vi.advanceTimersByTime(60000);
    expect(factory).toHaveBeenCalledTimes(1);
    expect(status).toHaveBeenCalledWith("session_expired");
    live.stop();
  });
});
