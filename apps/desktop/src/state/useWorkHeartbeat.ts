import { useEffect, useRef } from "react";
import type { Workspace } from "../types";
import { heartbeatActivity } from "./worktime";

export function useWorkHeartbeat(
  workspace: Workspace | null,
  api: <T>(path: string, method?: string, body?: unknown) => Promise<T>,
  refresh: () => Promise<void>,
  notice: (message: string) => void,
) {
  const current = useRef(workspace);
  current.current = workspace;
  const touched = useRef(Date.now());
  useEffect(() => {
    if (workspace?.user.presence.status === "ready")
      touched.current = Date.now();
  }, [workspace?.user.presence.status]);
  useEffect(() => {
    let busy = false,
      disposed = false,
      wasWorking = false;
    touched.current = Date.now();
    const touch = () => {
      touched.current = Date.now();
    };
    // Only a timestamp is held in memory; no keys, text or interaction history is collected.
    window.addEventListener("pointerdown", touch, { passive: true });
    window.addEventListener("keydown", touch);
    window.addEventListener("scroll", touch, { passive: true, capture: true });
    const timer = setInterval(() => {
      const w = current.current;
      if (!w || busy || disposed) return;
      const activity = heartbeatActivity(
        { presence: w.user.presence.status, repId: w.user.id, calls: w.calls },
        Date.now() - touched.current,
        Date.now(),
      );
      if (activity === null && !wasWorking) return;
      busy = true;
      void api<{ presence: { status: string } }>("/v1/rep/heartbeat", "POST", {
        activity: activity ?? "idle",
      })
        .then(async (result) => {
          if (disposed) return;
          wasWorking = activity !== null && activity !== "idle";
          if (activity === "idle" && result.presence.status === "paused")
            notice(
              "Dialing paused after 15 minutes without activity. Go ready when you return.",
            );
          if (activity === "idle") await refresh();
        })
        .catch(() => {
          if (!disposed) void refresh().catch(() => {});
        })
        .finally(() => {
          busy = false;
        });
    }, 60_000);
    return () => {
      disposed = true;
      clearInterval(timer);
      window.removeEventListener("pointerdown", touch);
      window.removeEventListener("keydown", touch);
      window.removeEventListener("scroll", touch, true);
    };
  }, [api, refresh, notice]);
}
