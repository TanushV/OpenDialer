import { useEffect, useState } from "react";
import { OpenFile } from "../components/OpenFile";
import { useWorkspace } from "../context";
import { parseCSV } from "../state/csv";
import { Button, Panel } from "../ui/kit";

interface TestList {
  list_name: string;
  contacts: { id: string; name: string; phone: string }[];
}

export function TestListSettings({
  onNavigate,
}: {
  onNavigate: (page: "Leads" | "Dialer") => void;
}) {
  const { api, refresh, run, snapshot } = useWorkspace();
  const [saved, setSaved] = useState<TestList | null>(null);
  const [draft, setDraft] = useState<Record<string, string>[] | null>(null);
  const [busy, setBusy] = useState(false);
  const active = Boolean(snapshot.test_run || snapshot.test_phone);
  useEffect(() => {
    let current = true;
    void run(async () => {
      const data = await api<TestList>("/v1/settings/test-list");
      if (current) setSaved(data);
    });
    return () => {
      current = false;
    };
  }, [api, run]);

  async function save() {
    setBusy(true);
    try {
      setSaved(
        await api<TestList>("/v1/settings/test-list", "PUT", { rows: draft }),
      );
      setDraft(null);
      await refresh();
    } finally {
      setBusy(false);
    }
  }
  return (
    <Panel className="padded" aria-label="Test list">
      <h2>Test list</h2>
      <p className="muted">
        Use a CSV with a phone column. Add name, company, and domain for
        personalized scripts. Include only phones you have permission to test.
      </p>
      <div className="button-row">
        <OpenFile
          label="Open test list CSV"
          accept=".csv,text/csv"
          disabled={active || busy}
          onOpen={(file) =>
            void run(async () => {
              setDraft(null);
              const rows = parseCSV(await file.text());
              if (!rows.length)
                throw new Error("Include at least one test phone.");
              setDraft(rows);
            })
          }
        />
        {draft && (
          <Button
            variant="secondary"
            disabled={busy || active}
            onClick={() => void run(save)}
          >
            Save test list
          </Button>
        )}
      </div>
      {draft && (
        <p className="muted">
          {draft.length} rows to save:{" "}
          {draft.map((row) => row.phone).join(", ")}
        </p>
      )}
      {saved && saved.contacts.length > 0 && (
        <>
          <p>{saved.contacts.length} test phones</p>
          <ul>
            {saved.contacts.map((row) => (
              <li key={row.id}>
                {row.name} · {row.phone}
              </li>
            ))}
          </ul>
          <div className="button-row">
            <Button variant="text" onClick={() => onNavigate("Leads")}>
              Generate scripts in Leads
            </Button>
            <Button
              disabled={busy || Boolean(draft)}
              onClick={() =>
                void run(async () => {
                  setBusy(true);
                  try {
                    if (!snapshot.test_run)
                      await api("/v1/settings/test-list/start", "POST");
                    await refresh();
                    onNavigate("Dialer");
                  } finally {
                    setBusy(false);
                  }
                })
              }
            >
              Open test list in Dialer
            </Button>
          </div>
        </>
      )}
    </Panel>
  );
}
