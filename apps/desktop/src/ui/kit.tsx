/**
 * The UI kit (R5.1): eight components with one look. New screens build from these; older pages move onto them
 * as Phase 10 rebuilds each page.
 */
import "./kit.css";
import type {
  ButtonHTMLAttributes,
  HTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
} from "react";

type Variant = "primary" | "secondary" | "danger" | "text";

export function Button({
  variant = "secondary",
  kbd,
  className = "",
  children,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant;
  kbd?: string;
}) {
  return (
    <button {...props} className={`ui-button ${variant} ${className}`.trim()}>
      {children}
      {kbd && (
        <>
          {" "}
          <kbd className="ui-kbd">{kbd}</kbd>
        </>
      )}
    </button>
  );
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="ui-field">
      {label}
      {children}
      {hint && <span className="ui-hint">{hint}</span>}
    </label>
  );
}

export function Select({
  label,
  options,
  ...props
}: SelectHTMLAttributes<HTMLSelectElement> & {
  label: string;
  options: [string, string][];
}) {
  return (
    <Field label={label}>
      <select {...props}>
        {options.map(([value, name]) => (
          <option key={value} value={value}>
            {name}
          </option>
        ))}
      </select>
    </Field>
  );
}

/** A checkbox and its label on one line (the old global label rule stacked them). */
export function Check({
  label,
  checked,
  onChange,
  disabled,
}: {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <label className="ui-check">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      {label}
    </label>
  );
}

export function Status({
  tone = "neutral",
  children,
  label,
}: {
  tone?: "neutral" | "good" | "warn" | "bad";
  children?: ReactNode;
  label?: string;
}) {
  return (
    <span className={`ui-status ${tone}`} role="status" aria-label={label}>
      {children}
    </span>
  );
}

export function Panel({
  title,
  children,
  className = "",
  ...props
}: HTMLAttributes<HTMLElement> & { title?: string }) {
  return (
    <section {...props} className={`ui-panel ${className}`.trim()}>
      {title && <h2 className="ui-panel-title">{title}</h2>}
      {children}
    </section>
  );
}

export function Toast({ children }: { children: ReactNode }) {
  return (
    <div className="ui-toast" role="status">
      {children}
    </div>
  );
}

export function Table({
  columns,
  rows,
}: {
  columns: string[];
  rows: ReactNode[][];
}) {
  return (
    <table className="ui-table">
      <thead>
        <tr>
          {columns.map((column) => (
            <th className="ui-cell" key={column}>
              {column}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row, r) => (
          <tr key={r}>
            {row.map((cell, c) => (
              <td className="ui-cell" key={c}>
                {cell}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
