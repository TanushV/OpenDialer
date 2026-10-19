import { useEffect, useState } from "react";
import { label, useWorkspace } from "../context";
import { DncScreeningPanel } from "./DncScreeningPanel";
import { localEdit } from "../api/client";

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
  dnc_refresh: {
    enabled: boolean;
    interval_hours: number;
    batch_size: number;
    max_run_usd: string;
    max_day_usd: string;
  };
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
export function OperatingPolicySettings() {
  const { api, run, notice } = useWorkspace();
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [key, setKey] = useState("");
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
        dnc_seller_authorization?: {
          reference: string;
          npas: string[];
          expires_at: string;
          source_refreshed_at?: string;
          source_access_verified: boolean;
        };
      }>("/v1/admin/settings");
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
      <h2>No subscriptions</h2>
      <p>
        Use existing numbers and pay for permitted usage. New number rentals and
        Telnyx's reputation subscription are blocked. Existing carrier rentals
        remain payable.
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
                    dnc_refresh: policy.dnc_refresh,
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
                min={3}
                max={60}
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
          <h3>Scheduled prepaid screening</h3>
          <p className="fine">
            Uses only existing authorized credits. Each run checks local
            opt-outs and current cache first. Unknown charges require review;
            they are never automatically retried. No account is funded by this
            setting.
          </p>
          <label className="check-label">
            <input
              type="checkbox"
              checked={policy.dnc_refresh.enabled}
              onChange={(e) =>
                setPolicy({
                  ...policy,
                  dnc_refresh: {
                    ...policy.dnc_refresh,
                    enabled: e.target.checked,
                  },
                })
              }
            />
            Authorize scheduled prepaid DNC checks
          </label>
          <div className="two-column">
            {(
              [
                ["max_run_usd", "DNC maximum per run (USD)"],
                ["max_day_usd", "DNC maximum per day (USD)"],
              ] as const
            ).map(([field, title]) => (
              <label key={field}>
                {title}
                <input
                  type="number"
                  min={policy.dnc_refresh.enabled ? "0.000001" : "0"}
                  step="0.000001"
                  required
                  value={policy.dnc_refresh[field]}
                  onChange={(e) =>
                    setPolicy({
                      ...policy,
                      dnc_refresh: {
                        ...policy.dnc_refresh,
                        [field]: e.target.value,
                      },
                    })
                  }
                />
              </label>
            ))}
            <label>
              Screening interval (hours)
              <input
                type="number"
                min={24}
                max={168}
                required
                value={policy.dnc_refresh.interval_hours}
                onChange={(e) =>
                  setPolicy({
                    ...policy,
                    dnc_refresh: {
                      ...policy.dnc_refresh,
                      interval_hours: Number(e.target.value),
                    },
                  })
                }
              />
            </label>
            <label>
              Endpoints per screening page
              <input
                type="number"
                min={1}
                max={500}
                required
                value={policy.dnc_refresh.batch_size}
                onChange={(e) =>
                  setPolicy({
                    ...policy,
                    dnc_refresh: {
                      ...policy.dnc_refresh,
                      batch_size: Number(e.target.value),
                    },
                  })
                }
              />
            </label>
            <label>
              Maximum national source age (days)
              <input
                type="number"
                min={1}
                max={31}
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
      <details>
        <summary>PAYG cost outline</summary>
        {policy && (
          <table>
            <thead>
              <tr>
                <th>Component</th>
                <th>Published estimate</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>Voice</td>
                <td>
                  ${policy.costs.voice.api_per_minute_usd}/min API + destination
                  termination; agent leg billed separately
                </td>
              </tr>
              <tr>
                <td>Transcription</td>
                <td>
                  Soniox ${policy.costs.stt.soniox_per_minute_usd}/min; Parakeet
                  ${policy.costs.stt.parakeet_per_minute_usd}/min. Verify tracks
                  and rounding.
                </td>
              </tr>
              <tr>
                <td>Cerebras via OpenRouter</td>
                <td>
                  ${policy.costs.cerebras.input_per_million_usd}/M input, $
                  {policy.costs.cerebras.output_per_million_usd}/M output tokens
                  + platform fees (
                  {policy.costs.cerebras.platform_fee_fraction * 100}%
                  reference)
                </td>
              </tr>
              <tr>
                <td>Prepaid DNC</td>
                <td>
                  ${policy.costs.dnc.prepaid_minimum_usd} /{" "}
                  {policy.costs.dnc.credits.toLocaleString("en-US")} queries; no
                  recurring charge. Seller access is separate.
                </td>
              </tr>
              <tr>
                <td>Numbers</td>
                <td>
                  Existing rental obligations remain; new recurring commitments
                  blocked
                </td>
              </tr>
            </tbody>
          </table>
        )}
        <p className="fine">
          Reference prices checked {policy?.rates_checked_at || "unavailable"}.
          Taxes, route fees and actual provider receipts govern. No account is
          funded automatically.
        </p>
      </details>
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
              {policy.costs[rateComponent].checked_at}.{" "}
              <a
                href={policy.costs[rateComponent].source_url}
                target="_blank"
                rel="noreferrer"
              >
                Published source
              </a>
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
          </form>
          <table>
            <thead>
              <tr>
                <th>Component / unit</th>
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
                  <td>
                    {label(cost.classification)}. {cost.basis} Minimum funding:{" "}
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
        </details>
      )}
      <h3>Prepaid DNC screening</h3>
      <p>
        National screening and the local opt-out registry are checked before
        every US call. Missing, stale or uncovered data blocks dialing. A
        prepaid API does not replace seller authorization for registry access.
      </p>
      <label>
        Prepaid DNC API key
        <input
          type="password"
          autoComplete="off"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          placeholder="Enter to securely replace the key"
        />
      </label>
      <button
        className="secondary"
        disabled={!key}
        onClick={() =>
          void run(async () => {
            await api("/v1/admin/settings", "PUT", { dnc_api_key: key });
            setKey("");
            notice(
              "DNC key stored securely; source authorization is still required",
            );
          })
        }
      >
        Save DNC key
      </button>
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
          onClick={() =>
            void run(async () => {
              await api("/v1/admin/settings", "PUT", {
                dnc_seller_authorization: {
                  ...authorization,
                  npas: authorization.npas.split(/[,\s]+/).filter(Boolean),
                  source_refreshed_at:
                    authorization.source_refreshed_at || null,
                },
              });
              notice("Seller authorization saved");
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
