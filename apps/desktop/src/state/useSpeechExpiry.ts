import { useEffect, type Dispatch, type SetStateAction } from "react";
import { expireSpeech, watchSpeechExpiry } from "./privacy";
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
    const cancel = watchSpeechExpiry(value, purge);
    window.addEventListener("pageshow", purge);
    document.addEventListener("visibilitychange", purge);
    return () => {
      cancel();
      window.removeEventListener("pageshow", purge);
      document.removeEventListener("visibilitychange", purge);
    };
  }, [value, setValue]);
}
