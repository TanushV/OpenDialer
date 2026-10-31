import { localEdit } from "../api/client";
import { useEffect, useRef, useState } from "react";
import { label, useWorkspace } from "../context";
import { limits, useDefinitions } from "../state/definitions";
import { CredentialField } from "./CredentialField";
import { DncScreeningPanel } from "./DncScreeningPanel";

type AccountRate = {
  values: Record<string, string>;
  source_reference: string;
  checked_at: string;
};
type Cost = {
  unit: string;
  currency: string;
  source_url: string;
  checked_at: string;
  classification: string;
  minimum_funding_usd: number | null;
  rate_fields: string[];
  minimum_rate: string;
  basis?: string;
  account_rate_override: AccountRate | null;
};
type Policy = {
  version: number;
  dnc_source_max_age_days: number;
  auto_coach_interval_seconds: number;
  transcription_privacy_verified: boolean;
  transcription_privacy_reference: string;
  rates_checked_at: string;
  account_rates: Record<string, AccountRate>;
  costs: Record<string, Cost> & {
    voice: Cost & { api_per_minute_usd: number };
    stt: Cost & {
      soniox_per_minute_usd: number;
      parakeet_per_minute_usd: number;
    };
    cerebras: Cost & {
      input_per_million_usd: number;
      output_per_million_usd: number;
      platform_fee_fraction: number;
    };
    dnc: Cost & { prepaid_minimum_usd: number; credits: number };
  };
};
const money = (value: number | string | null | undefined) =>
  value == null ? "quote required" : `$${value}`;
/** A component's published reference rate in plain words; generic fields otherwise. */
function reference(name: string, cost: Cost) {
  const c = cost as Cost & Record<string, number | null>;
  switch (name) {
    case "voice":
      return `${money(c.api_per_minute_usd)}/min API + destination termination; agent leg billed separately`;
    case "stt":
      return `Soniox ${money(c.soniox_per_minute_usd)}/min; Parakeet ${money(c.parakeet_per_minute_usd)}/min. Verify tracks and rounding.`;
    case "cerebras":
      return `${money(c.input_per_million_usd)}/M input, ${money(c.output_per_million_usd)}/M output tokens + platform fees (${(c.platform_fee_fraction ?? 0) * 100}% reference)`;
    case "dnc":
      return `${money(c.prepaid_minimum_usd)} / ${(c.credits ?? 0).toLocaleString("en-US")} queries; no recurring charge. Seller access is separate.`;
    case "number_rental":
      return `From ${money(c.from_monthly_usd)}/month. Existing rental obligations remain; new rentals require explicit approval in Numbers.`;
    default:
      return cost.rate_fields.length
        ? cost.rate_fields
            .map((field) => `${label(field)}: ${money(c[field])}`)
            .join("; ")
        : "No usage charge";
  }
}

export function OperatingPolicySettings() {
  const { api, run, notice } = useWorkspace();
  const bounds = useDefinitions();
  const o = bounds?.operating;
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [credential, setCredential] = useState<{
    version: number;
    configured: Record<string, boolean>;
  } | null>(null);
  const authorizationEdit = useRef<{ body: string; id: string } | null>(null);
  const [authorizationBusy, setAuthorizationBusy] = useState(false);
  const [rateComponent, setRateComponent] = useState("dnc");
  const [rateDraft, setRateDraft] = useState<Policy["account_rates"][string]>({
    values: {},
    source_reference: "",
    checked_at: "",
  });
  useEffect(() => {
    setRateDraft(
      policy?.account_rates[rateComponent] || {
        values: {},
        source_reference: "",
        checked_at: "",
      },
    );
  }, [policy?.version, rateComponent]);
  const [authorization, setAuthorization] = useState({
    reference: "",
    npas: "",
    expires_at: "",
    source_refreshed_at: "",
    source_access_verified: false,
  });
  useEffect(() => {
    void run(async () => {
      setPolicy(await api<Policy>("/v1/admin/operating-policy"));
      const settings = await api<{
        version: number;
        configured: Record<string, boolean>;
        dnc_seller_authorization?: {
          reference: string;
          npas: string[];
          expires_at: string;
          source_refreshed_at?: string;
          source_access_verified: boolean;
        };
      }>("/v1/admin/settings");
      setCredential(settings);
      const auth = settings.dnc_seller_authorization;
      if (auth)
        setAuthorization({
          ...auth,
          npas: auth.npas.join(", "),
          source_refreshed_at: auth.source_refreshed_at || "",
        });
    });
  }, []);
  return (
    <section className="card padded operating-policy">
      <span className="eyebrow">PRIVACY & USAGE COSTS</span>
      <h2>Spending permissions</h2>
      <p>
        Number rentals require saved limits and approval of each quote in
        Numbers. Telnyx's reputation subscription remains blocked. Existing
        carrier rentals remain payable.
      </p>
      <p>
        <strong>Raw calls and transcripts: five minutes at most.</strong> Call
        recording is disabled. Only derived notes, outcomes, statistics and your
        own notes remain. There is no calendar or CRM synchronization; reps book
        meetings themselves.
      </p>
      {policy && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void run(async () => {
              setPolicy(
                await localEdit<Policy>(
                  api,
                  "/v1/admin/operating-policy",
                  "PUT",
                  {
                    request_id: crypto.randomUUID(),
                    expected_version: policy.version,
                    auto_coach_interval_seconds:
                      policy.auto_coach_interval_seconds,
                    transcription_privacy_verified:
                      policy.transcription_privacy_verified,
                    transcription_privacy_reference:
                      policy.transcription_privacy_reference,
                    dnc_source_max_age_days: policy.dnc_source_max_age_days,
                  },
                ),
              );
              notice("Privacy and usage policy saved");
            });
          }}
        >
          <div className="two-column">
            <label>
              Seconds between rep-only context refreshes
              <input
                type="number"
                {...limits(o?.auto_coach_interval_seconds)}
                value={policy.auto_coach_interval_seconds}
                onChange={(e) =>
                  setPolicy({
                    ...policy,
                    auto_coach_interval_seconds: Number(e.target.value),
                  })
                }
              />
            </label>
            <label>
              STT account retention evidence
              <input
                value={policy.transcription_privacy_reference}
                onChange={(e) =>
                  setPolicy({
                    ...policy,
                    transcription_privacy_reference: e.target.value,
                  })
                }
                placeholder="Reference to verified provider/account retention terms"
              />
            </label>
          </div>
          <label className="check-label">
            <input
              type="checkbox"
              checked={policy.transcription_privacy_verified}
              onChange={(e) =>
                setPolicy({
                  ...policy,
                  transcription_privacy_verified: e.target.checked,
                })
              }
            />{" "}
            Provider processing retention has been verified for this account
          </label>
          <p className="fine">
            Production transcription stays disabled without this evidence. Local
            deletion cannot certify upstream deletion. Cerebras uses ZDR with
            data collection denied and no alternate-provider fallback.
          </p>
          <div className="two-column">
            <label>
              Maximum national source age (days)
              <input
                type="number"
                {...limits(o?.dnc_source_max_age_days)}
                required
                value={policy.dnc_source_max_age_days}
                onChange={(e) =>
                  setPolicy({
                    ...policy,
                    dnc_source_max_age_days: Number(e.target.value),
                  })
                }
              />
            </label>
          </div>
          <button className="secondary" type="submit">
            Save privacy and usage policy
          </button>
          <button
            type="button"
            className="text-button"
            onClick={() =>
              void run(async () =>
                setPolicy(await api<Policy>("/v1/admin/operating-policy")),
              )
            }
          >
            Reload privacy and usage policy
          </button>
        </form>
      )}
      {policy && (
        <details>
          <summary>PAYG cost outline</summary>
          <table aria-label="Usage cost register">
            <thead>
              <tr>
                <th>Component / unit</th>
                <th>Published reference</th>
                <th>Cost boundary</th>
                <th>Source</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(policy.costs).map(([name, cost]) => (
                <tr key={name}>
                  <td>
                    {label(name)} · {cost.unit}
                  </td>
                  <td>{reference(name, cost)}</td>
                  <td>
                    {label(cost.classification)}.{" "}
                    {cost.basis ? cost.basis + ". " : ""}Minimum funding:{" "}
                    {cost.minimum_funding_usd === null
                      ? "account-dependent"
                      : `$${cost.minimum_funding_usd}`}
                    .
                  </td>
                  <td>
                    <a href={cost.source_url} target="_blank" rel="noreferrer">
                      Reference
                    </a>{" "}
                    · {cost.checked_at}
                    {cost.account_rate_override &&
                      ` · Account rate: ${cost.account_rate_override.source_reference}`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="fine">
            Reference prices checked {policy.rates_checked_at || "unavailable"}.
            Taxes, route fees and actual provider receipts govern. No account is
            funded automatically.
          </p>
        </details>
      )}
      {policy && (
        <details>
          <summary>Account rate estimates</summary>
          <p className="fine">
            Published reference rates remain separate from account estimates and
            actual charges.
          </p>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void run(async () => {
                const values = Object.fromEntries(
                  Object.entries(rateDraft.values).filter(
                    ([, value]) => value !== "",
                  ),
                );
                const saved = await localEdit<Policy>(
                  api,
                  "/v1/admin/operating-policy",
                  "PUT",
                  {
                    request_id: crypto.randomUUID(),
                    expected_version: policy.version,
                    account_rates: {
                      ...policy.account_rates,
                      [rateComponent]: { ...rateDraft, values },
                    },
                  },
                );
                setPolicy(saved);
                notice("Account rate estimate saved");
              });
            }}
          >
            <label>
              Rate component
              <select
                value={rateComponent}
                onChange={(e) => setRateComponent(e.target.value)}
              >
                {Object.entries(policy.costs)
                  .filter(([, cost]) => cost.rate_fields.length)
                  .map(([name]) => (
                    <option key={name} value={name}>
                      {label(name)}
                    </option>
                  ))}
              </select>
            </label>
            <p className="fine">
              Unit: {policy.costs[rateComponent].unit}. Reference checked{" "}
              {policy.costs[rateComponent].checked_at}.
            </p>
            <div className="two-column">
              {policy.costs[rateComponent].rate_fields.map((field) => (
                <label key={field}>
                  {label(field)}
                  <input
                    type="number"
                    min={policy.costs[rateComponent].minimum_rate}
                    step="0.000000001"
                    value={rateDraft.values[field] ?? ""}
                    onChange={(e) =>
                      setRateDraft({
                        ...rateDraft,
                        values: {
                          ...rateDraft.values,
                          [field]: e.target.value,
                        },
                      })
                    }
                  />
                </label>
              ))}
              <label>
                Rate invoice or quote reference
                <input
                  required
                  maxLength={1000}
                  value={rateDraft.source_reference}
                  onChange={(e) =>
                    setRateDraft({
                      ...rateDraft,
                      source_reference: e.target.value,
                    })
                  }
                />
              </label>
              <label>
                Account rate checked on
                <input
                  required
                  type="date"
                  value={rateDraft.checked_at}
                  onChange={(e) =>
                    setRateDraft({ ...rateDraft, checked_at: e.target.value })
                  }
                />
              </label>
            </div>
            <button
              type="submit"
              className="secondary"
              disabled={!Object.values(rateDraft.values).some((v) => v !== "")}
            >
              Save account rate estimate
            </button>
            {!Object.values(rateDraft.values).some((v) => v !== "") && (
              <p className="fine">Enter at least one account rate to save.</p>
            )}
          </form>
        </details>
      )}
      <h3>Prepaid DNC screening</h3>
      <p>
        National screening and the local opt-out registry are checked before
        every US call. Missing, stale or uncovered data blocks dialing. A
        prepaid API does not replace seller authorization for registry access.
      </p>
      <CredentialField
        field="dnc_api_key"
        title="Prepaid DNC API key"
        version={credential?.version}
        configured={credential?.configured.dnc_api_key}
        onSaved={(result) => {
          setCredential(result);
          notice("DNC key saved; seller authorization is still required.");
        }}
      />
      <details>
        <summary>Seller authorization and source freshness</summary>
        <label>
          Seller access reference
          <input
            value={authorization.reference}
            onChange={(e) =>
              setAuthorization({ ...authorization, reference: e.target.value })
            }
          />
        </label>
        <label>
          Covered prospect area codes
          <input
            value={authorization.npas}
            onChange={(e) =>
              setAuthorization({ ...authorization, npas: e.target.value })
            }
          />
        </label>
        <label>
          Authorization expiry (ISO timestamp)
          <input
            value={authorization.expires_at}
            placeholder="2026-12-01T00:00:00Z"
            onChange={(e) =>
              setAuthorization({ ...authorization, expires_at: e.target.value })
            }
          />
        </label>
        <label>
          Registry source refresh (ISO timestamp)
          <input
            value={authorization.source_refreshed_at}
            onChange={(e) =>
              setAuthorization({
                ...authorization,
                source_refreshed_at: e.target.value,
              })
            }
          />
        </label>
        <label className="check-label">
          <input
            type="checkbox"
            checked={authorization.source_access_verified}
            onChange={(e) =>
              setAuthorization({
                ...authorization,
                source_access_verified: e.target.checked,
              })
            }
          />{" "}
          I verified this seller's access and the provider's source
        </label>
        <button
          className="secondary"
          disabled={!credential || authorizationBusy}
          onClick={() =>
            void run(async () => {
              if (!credential) return;
              const data = {
                expected_version: credential.version,
                dnc_seller_authorization: {
                  ...authorization,
                  npas: authorization.npas.split(/[,\s]+/).filter(Boolean),
                  source_refreshed_at:
                    authorization.source_refreshed_at || null,
                },
              };
              const body = JSON.stringify(data);
              if (authorizationEdit.current?.body !== body)
                authorizationEdit.current = { body, id: crypto.randomUUID() };
              setAuthorizationBusy(true);
              try {
                setCredential(
                  await api("/v1/admin/settings", "PUT", {
                    ...data,
                    request_id: authorizationEdit.current.id,
                  }),
                );
                authorizationEdit.current = null;
                notice("Seller authorization saved");
              } finally {
                setAuthorizationBusy(false);
              }
            })
          }
        >
          Save seller authorization
        </button>
      </details>
      <DncScreeningPanel />
    </section>
  );
}
