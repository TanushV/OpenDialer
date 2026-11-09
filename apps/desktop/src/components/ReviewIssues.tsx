import { useEffect, useState } from "react";
import { useWorkspace } from "../context";
import { Button, Panel } from "../ui/kit";

export interface Issue {
  code: string;
  title: string;
  detail: string | null;
  count: number | null;
  level: "fix" | "info";
  fix: { page: string; label: string } | null;
}

const PAGES: Record<string, string> = {
  settings: "Settings",
  numbers: "Numbers",
  leads: "Leads",
  dialer: "Dialer",
};

/** Review opens with what needs you and the one thing to do about each. */
export function ReviewIssues({
  onNavigate,
}: {
  onNavigate?: (page: string) => void;
}) {
  const { api, snapshot } = useWorkspace();
  const [issues, setIssues] = useState<Issue[] | null>(null);
  // Re-read when the shown workspace changes (a saved outcome, a synced list), not on a timer.
  useEffect(() => {
    let current = true;
    void api<{ issues: Issue[] }>("/v1/review/issues")
      .then((body) => current && setIssues(body.issues))
      .catch(() => current && setIssues(null));
    return () => {
      current = false;
    };
  }, [api, snapshot]);
  if (!issues) return null;
  function go(page: string) {
    if (page === "review") {
      document
        .querySelector("[data-review-section]")
        ?.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    onNavigate?.(PAGES[page] ?? page);
  }
  return (
    <Panel title="Needs you" aria-label="Needs you" className="review-issues">
      {issues.length === 0 ? (
        <p className="review-issues-clear">Nothing needs you right now.</p>
      ) : (
        <ul>
          {issues.map((issue) => (
            <li
              key={issue.code}
              className={`review-issue ${issue.level}`}
              data-issue={issue.code}
            >
              <div>
                <strong>{issue.title}</strong>
                {issue.detail && <small>{issue.detail}</small>}
              </div>
              {issue.fix && (
                <Button onClick={() => go(issue.fix!.page)}>
                  {issue.fix.label}
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}
