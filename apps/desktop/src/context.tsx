import { createContext, useContext } from "react";
import type { Session, Workspace } from "./types";
import type { LiveEvent } from "./api/live";
import type { ProviderCredential } from "./api/client";
export interface WorkspaceContext {
  snapshot: Workspace;
  session: Session;
  api: <T>(path: string, method?: string, body?: unknown) => Promise<T>;
  refresh: () => Promise<void>;
  notice: (message: string) => void;
  run: (work: () => Promise<void>) => Promise<void>;
  subscribe: (handler: (events: LiveEvent[]) => void) => () => void;
  liveStatus: string;
  configureCredential: (
    field: ProviderCredential,
    version: number,
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
export function date(value?: string | null) {
  return value
    ? new Date(value).toLocaleString([], {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      })
    : "Needs confirmation";
}
