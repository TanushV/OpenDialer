import { useEffect, useState } from "react";
import { useWorkspace } from "../context";

/** One field's validation bound, read from the backend model that validates the write. */
export interface FieldBound {
  type?: string;
  min?: number;
  max?: number;
  min_exclusive?: number;
  max_exclusive?: number;
  min_length?: number;
  max_length?: number;
  min_items?: number;
  max_items?: number;
  enum?: (string | number | boolean)[];
  default?: unknown;
  items?: FieldBound;
}
export type Definitions = Record<string, Record<string, FieldBound>>;

const loaded = new WeakMap<object, Promise<Definitions>>();

/** Shared bounds and enums for every role; one request per signed-in authority. */
export function useDefinitions(): Definitions | null {
  const { api } = useWorkspace();
  const [value, setValue] = useState<Definitions | null>(null);
  useEffect(() => {
    let live = true;
    let pending = loaded.get(api);
    if (!pending) {
      pending = api<{ bounds: Definitions }>("/v1/definitions").then(
        (result) => result.bounds,
      );
      loaded.set(api, pending);
      // A failed read is retried by the next consumer instead of being cached.
      pending.catch(() => loaded.delete(api));
    }
    pending.then(
      (result) => live && setValue(result),
      () => {},
    );
    return () => {
      live = false;
    };
  }, [api]);
  return value;
}

/** Numeric input limits; an unloaded bound leaves validation to the authority. */
export function limits(bound?: FieldBound) {
  return {
    min: bound?.min ?? bound?.min_exclusive,
    max: bound?.max ?? bound?.max_exclusive,
  };
}

/** Enum options for a select, in the backend's declared order. */
export function options(bound?: FieldBound): string[] {
  return (bound?.enum ?? []).map(String);
}
