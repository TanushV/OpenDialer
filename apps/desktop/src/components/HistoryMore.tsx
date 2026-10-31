import { useWorkspace } from "../context";

export function HistoryMore({
  history,
  label,
}: {
  history: {
    items: unknown[];
    totalLabel?: string | null;
    cursor?: string | null;
    busy: boolean;
    load: (append?: boolean) => Promise<void>;
  };
  label: string;
}) {
  const { run } = useWorkspace();
  return (
    <div className="button-row">
      {history.items.length > 0 && (
        <span className="fine">
          Showing {history.items.length}
          {history.totalLabel != null ? ` of ${history.totalLabel}` : ""}
        </span>
      )}
      {history.cursor && (
        <button
          className="secondary small"
          disabled={history.busy}
          onClick={() => void run(() => history.load(true))}
        >
          {label}
        </button>
      )}
    </div>
  );
}
