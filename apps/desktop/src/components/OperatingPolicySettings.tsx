import { useEffect, useState } from "react";
import { useWorkspace } from "../context";
import { DncScreeningPanel } from "./DncScreeningPanel";

type Policy = {
  auto_coach_interval_seconds: number;
  transcription_privacy_verified: boolean;
  transcription_privacy_reference: string;
  rates_checked_at: string;
  costs: {
    voice: { api_per_minute_usd: number };
    stt: { soniox_per_minute_usd: number; parakeet_per_minute_usd: number };
    cerebras: {
      input_per_million_usd: number;
      output_per_million_usd: number;
      platform_fee_fraction: number;
    };
    dnc: { prepaid_minimum_usd: number; credits: number };
  };
};
export function OperatingPolicySettings() {
  const { api, run, notice } = useWorkspace();
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [key, setKey] = useState("");
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
              await api("/v1/admin/operating-policy", "PUT", {
                auto_coach_interval_seconds: policy.auto_coach_interval_seconds,
                transcription_privacy_verified:
                  policy.transcription_privacy_verified,
                transcription_privacy_reference:
                  policy.transcription_privacy_reference,
              });
              notice("Privacy and helper pacing saved");
            });
          }}
        >
          <div className="two-column">
            <label>
              Minimum seconds between automatic helper requests
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
          <button className="secondary" type="submit">
            Save privacy and pacing
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
