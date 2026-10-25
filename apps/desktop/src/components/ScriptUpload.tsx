import { useEffect, useState } from "react";
import { isAdmin, useWorkspace } from "../context";
import { ScriptReader } from "./ScriptReader";
import type { Script } from "../types";

type Base = { version: number; template: Partial<Script> | null };
export function ScriptUpload() {
  const { api, run, notice, snapshot, subscribe } = useWorkspace();
  const [base, setBase] = useState<Base | null>(null);
  const [text, setText] = useState("");
  const [preview, setPreview] = useState<Partial<Script> | null>(null);
  const [editing, setEditing] = useState(false);
  const [editingVersion, setEditingVersion] = useState<number | null>(null);
  const load = async () =>
    setBase((await api<{ base: Base }>("/v1/optimization/script")).base);
  useEffect(() => {
    void run(load);
    return subscribe((events) => {
      if (events.some((e) => e.type === "optimization.updated")) void run(load);
    });
  }, [api, run, subscribe]);
  function change(value: string) {
    setText(value);
    setPreview(null);
  }
  return (
    <section className="card padded" aria-label="Your script">
      <div className="section-heading">
        <h2>Your script</h2>
        {isAdmin(snapshot.user.role) && (
          <button
            className="secondary"
            disabled={!base}
            onClick={() => {
              if (!editing) {
                setEditingVersion(base!.version);
                setPreview(null);
              }
              setEditing(!editing);
            }}
          >
            {editing ? "Close editor" : "Upload or paste script"}
          </button>
        )}
      </div>
      <p className="muted small">
        Use headings for the opening, questions, offers and objections. Each
        becomes a section you can read while calling.
      </p>
      {editing && base?.version !== editingVersion && (
        <p role="status">
          The script changed while you were editing. Reopen the editor to review
          the latest version.
        </p>
      )}
      {editing && (
        <>
          <label>
            Markdown or text file
            <input
              type="file"
              accept=".md,.markdown,.txt,text/plain,text/markdown"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file)
                  void run(async () => {
                    if (file.size > 40000)
                      throw new Error("Keep the script under 40 KB.");
                    change(await file.text());
                  });
              }}
            />
          </label>
          <label>
            Script text
            <textarea
              rows={12}
              value={text}
              onChange={(e) => change(e.target.value)}
              placeholder={
                "## Opening\nHi {name}, this is {rep} from {seller}.\n\n## Objection: busy\nWhen would be better?"
              }
            />
          </label>
          <button
            className="secondary"
            disabled={!text.trim() || !base}
            onClick={() =>
              void run(async () => {
                const result = await api<{ template: Partial<Script> }>(
                  "/v1/admin/script/import",
                  "POST",
                  {
                    markdown: text,
                    expected_version: editingVersion,
                    preview: true,
                  },
                );
                setPreview(result.template);
              })
            }
          >
            Preview sections
          </button>
          {preview && (
            <>
              <ScriptReader script={preview} />
              <button
                className="primary"
                onClick={() =>
                  void run(async () => {
                    await api("/v1/admin/script/import", "POST", {
                      markdown: text,
                      expected_version: editingVersion,
                      preview: false,
                    });
                    await load();
                    setEditing(false);
                    setPreview(null);
                    notice("Script saved. New calls will use this version.");
                  })
                }
              >
                Save script
              </button>
            </>
          )}
        </>
      )}
      {!editing &&
        (base?.template ? (
          <ScriptReader script={base.template} />
        ) : (
          <p>
            Upload your script to get started. The approved introduction remains
            available until then.
          </p>
        ))}
    </section>
  );
}
