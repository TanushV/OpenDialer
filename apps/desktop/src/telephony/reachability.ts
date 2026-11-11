/** Publish the observed registration independently of a slow microphone or SDK connect. */
export function createReachabilityHeartbeat({
  registered,
  connect,
  publish,
  reportError,
}: {
  registered: () => boolean;
  connect: () => Promise<void>;
  publish: (registered: boolean) => Promise<unknown>;
  reportError: (error: unknown) => void;
}) {
  let stopped = false;
  let publishing = false;
  let connecting = false;
  let retryAt = 0;
  const report = async () => {
    if (stopped || publishing) return;
    publishing = true;
    try {
      await publish(registered());
    } catch (error) {
      reportError(error);
    } finally {
      publishing = false;
    }
  };
  return {
    async tick() {
      if (stopped) return;
      await report();
      if (stopped || connecting || registered() || Date.now() < retryAt) return;
      connecting = true;
      try {
        await connect();
      } catch (error) {
        retryAt = Date.now() + 5 * 60_000;
        if (!stopped) reportError(error);
      } finally {
        connecting = false;
      }
      if (registered()) await report();
    },
    stop() {
      stopped = true;
    },
  };
}
