import { useWorkspace } from "../context";

export function HistoryMore({
  history,
  label,
}: {
  history: {
    items: unknown[];
    total?: number | null;
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
        {history.total != null ? ` · ${history.total} records in scope` : ""}
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
