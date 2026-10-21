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
      <span className="fine">
        {history.items.length} displayed
        {history.totalLabel != null
          ? ` · ${history.totalLabel} records in scope`
          : ""}
      </span>
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
