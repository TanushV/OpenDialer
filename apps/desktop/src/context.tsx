import { createContext, useContext } from "react";
import type { Session, Workspace, Runtime } from "./types";
import type { LiveEvent } from "./api/live";
import type { ProviderCredential, RequestOptions } from "./api/client";
export interface WorkspaceContext {
  snapshot: Workspace;
  runtime?: Runtime | null;
  session: Session;
  api: <T>(
    path: string,
    method?: string,
    body?: unknown,
    options?: RequestOptions,
  ) => Promise<T>;
  refresh: () => Promise<void>;
  notice: (message: string) => void;
  run: (work: () => Promise<void>) => Promise<void>;
  subscribe: (handler: (events: LiveEvent[]) => void) => () => void;
  liveStatus: string;
  /** Open the follow-up list at one task. */
  openTask?: (id: string) => void;
  /** The task most recently opened with openTask. */
  focusTask?: string | null;
  configureCredential: (
    field: ProviderCredential,
    version: number,
    value: string,
  ) => Promise<{ version: number; configured: Record<string, boolean> }>;
}
export const Context = createContext<WorkspaceContext | null>(null);
export function useWorkspace() {
  const value = useContext(Context);
  if (!value) throw new Error("Workspace unavailable");
  return value;
}
export function label(value: string) {
  return value
    .replaceAll("_", " ")
    .toLowerCase()
    .replace(/^./, (c) => c.toUpperCase());
}
/** A short date; with `timeZone`, shown in that zone and labelled with it. */
export function date(value?: string | null, timeZone?: string) {
  if (!value) return "Needs confirmation";
  // Built from parts: WebKit and Chromium join the same format differently ("at" or ",").
  const parts = new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    ...(timeZone ? { timeZone, timeZoneName: "short" } : {}),
  }).formatToParts(new Date(value));
  const part = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((p) => p.type === type)?.value ?? "";
  const text = `${part("month")} ${part("day")}, ${part("hour")}:${part("minute")} ${part("dayPeriod")}`;
  return timeZone ? `${text} ${part("timeZoneName")}` : text;
}
/** The one fraction formatter: missing evidence is stated, never shown as zero. */
export function percent(
  value: number | null | undefined,
  missing = "Unknown",
  digits = 1,
) {
  return value == null ? missing : `${(100 * value).toFixed(digits)}%`;
}
/** Owners and administrators manage inventory, providers and policy; reps do not. */
