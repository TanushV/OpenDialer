import { expect, test, vi } from "vitest";
import { createReachabilityHeartbeat } from "../src/telephony/reachability";

test("a pending registration cannot block reachability or start a second registration", async () => {
  let registered = false;
  let release!: () => void;
  const pending = new Promise<void>((resolve) => (release = resolve));
  const connect = vi.fn(async () => {
    await pending;
    registered = true;
  });
  const publish = vi.fn(async (_registered: boolean) => {});
  const heartbeat = createReachabilityHeartbeat({
    registered: () => registered,
    connect,
    publish,
    reportError: vi.fn(),
  });

  const first = heartbeat.tick();
  await vi.waitFor(() => expect(connect).toHaveBeenCalledTimes(1));
  expect(publish).toHaveBeenLastCalledWith(false);
  await heartbeat.tick();
  expect(publish.mock.calls).toEqual([[false], [false]]);
  expect(connect).toHaveBeenCalledTimes(1);
  release();
  await first;
  expect(publish).toHaveBeenLastCalledWith(true);
});

test("a slow reachability request never overlaps itself", async () => {
  let release!: () => void;
  const publish = vi.fn(
    () => new Promise<void>((resolve) => (release = resolve)),
  );
  const heartbeat = createReachabilityHeartbeat({
    registered: () => true,
    connect: vi.fn(async () => {}),
    publish,
    reportError: vi.fn(),
  });
  const first = heartbeat.tick();
  await heartbeat.tick();
  expect(publish).toHaveBeenCalledTimes(1);
  heartbeat.stop();
  release();
  await first;
  await heartbeat.tick();
  expect(publish).toHaveBeenCalledTimes(1);
});
