import { expect, test } from "vitest";
import { ErrorBoundary } from "../src/components/ErrorBoundary";

test("a render crash becomes a recoverable error screen", () => {
  // R0.13: without a boundary a render crash left a blank window.
  const state = ErrorBoundary.getDerivedStateFromError(new Error("boom"));
  expect(state.error?.message).toBe("boom");
});
