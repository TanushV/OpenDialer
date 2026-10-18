import { useEffect, type Dispatch, type SetStateAction } from "react";
import { expireSpeech, speechDeadline } from "./privacy";
import type { Call } from "../types";

type View = Pick<Call, "transcript" | "coach" | "raw_evidence_status">;
export function useSpeechExpiry<T extends View>(
  value: T | null,
  setValue: Dispatch<SetStateAction<T | null>>,
) {
  useEffect(() => {
    if (!value) return;
    const purge = () =>
      setValue((current) => (current ? expireSpeech(current) : null));
    const delay = speechDeadline(value);
    const timer = delay === null ? undefined : setTimeout(purge, delay);
    window.addEventListener("pageshow", purge);
    document.addEventListener("visibilitychange", purge);
    return () => {
      clearTimeout(timer);
      window.removeEventListener("pageshow", purge);
      document.removeEventListener("visibilitychange", purge);
    };
  }, [value, setValue]);
}
