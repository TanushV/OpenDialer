import { Component, type ErrorInfo, type ReactNode } from "react";

type State = { error: Error | null };

/**
 * A render crash shows what happened and a way back instead of a blank window
 * (docs/REBUILD_PLAN.md R0.13). Calls in progress keep running on the carrier and backend.
 */
export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("OpenDialer render failure", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <main className="crash-screen" role="alert">
        <h1>Something went wrong on this screen</h1>
        <p>
          Any call in progress keeps going on the carrier. Reload to get back to
          the Dialer.
        </p>
        <pre>{this.state.error.message}</pre>
        <button className="primary" onClick={() => window.location.reload()}>
          Reload
        </button>
      </main>
    );
  }
}
