"""Bounded, descriptive learning from reviewed conversation observations.

No taxonomy, sentence bandit, or automatic policy changes. One independent contact
contributes once; discovery precedes a later, label-embargoed comparison period.
"""

import hashlib
import re
import unicodedata
from collections import Counter, defaultdict
from statistics import NormalDist

from app.optimization.numbering import timestamp, wilson


def normalized(value):
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", value or "").casefold()))


def aliases_for(vocabulary):
    aliases = {}
    for entry in vocabulary.get("aliases", []):
        key = (entry["field"], normalized(entry["source"]))
        target = normalized(entry["target"])
        if not key[1] or not target or key in aliases or key[1] == target:
            raise ValueError("Normalization requires distinct, unique source and target names")
        aliases[key] = target
    if any((field, target) in aliases for (field, _), target in aliases.items()):
        raise ValueError("Normalization chains or cycles are not allowed")
    return aliases


def active_observations(items, as_of, *, require_review=True):
    return [
        item
        for item in items
        if item.get("status", "active") == "active"
        and (not require_review or (item.get("review_status") and item.get("observed_at")))
        and item.get("review_status", "model_reviewed") in {"model_reviewed", "human_confirmed"}
        and (not item.get("observed_at") or timestamp(item["observed_at"]) <= as_of)
    ]


def _mix(row):
    features = row.get("features", {})
    return (
        row.get("segment", "unknown"),
        bool(features.get("requested_contact")),
        min(3, int(features.get("attempt_count") or 0)),
    )


def _comparison(rows, key, *, min_support, multiplicity):
    strata = defaultdict(lambda: ([], []))
    for row in rows:
        keys = row["keys"]
        if key in keys:
            strata[_mix(row)][0].append(row["conversion"])
        elif any(concept == key[0] for concept, _ in keys):
            # No observation of a concept is not proof that it was absent.
            strata[_mix(row)][1].append(row["conversion"])
    overlap = [(a, b) for a, b in strata.values() if len(a) >= 3 and len(b) >= 3]
    exposed, reference = sum(len(a) for a, _ in overlap), sum(len(b) for _, b in overlap)
    result = {
        "exposed": exposed,
        "reference": reference,
        "comparable_strata": len(overlap),
        "difference": None,
        "difference_low": None,
        "difference_high": None,
    }
    if min(exposed, reference) < min_support:
        return result
    # Simultaneous intervals across two groups, strata and all discovery candidates.
    # Wilson limits remain approximate; Bonferroni controls multiplicity, not confounding.
    z = NormalDist().inv_cdf(1 - 0.05 / (4 * max(1, multiplicity) * len(overlap)))
    values = []
    for a, b in overlap:
        a_low, a_high = wilson(sum(a), len(a), z=z)
        b_low, b_high = wilson(sum(b), len(b), z=z)
        values.append(
            (min(len(a), len(b)), sum(a) / len(a) - sum(b) / len(b), a_low - b_high, a_high - b_low)
        )
    denominator = sum(v[0] for v in values)
    result.update(
        {
            name: sum(v[0] * v[index] for v in values) / denominator
            for index, name in enumerate(("", "difference", "difference_low", "difference_high"))
            if index
        }
    )
    return result


def conversation_associations(rows, *, as_of, min_support=10, vocabulary=None):
    if not 10 <= min_support <= 1000:
        raise ValueError("At least ten independent contacts are required")
    vocabulary = vocabulary or {"version": 1, "aliases": []}
    aliases = aliases_for(vocabulary)
    names = {(a["field"], normalized(a["target"])): a["target"] for a in vocabulary.get("aliases", [])}
    eligible, seen = [], set()
    ordered = sorted(rows, key=lambda r: (r.get("at", ""), r.get("call_id", "")))[-20000:]
    parent = {}

    def root(key):
        parent.setdefault(key, key)
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for row in ordered:
        if row.get("lead_id") and timestamp(row.get("at")) and timestamp(row["at"]) <= as_of:
            lead = ("lead", row["lead_id"])
            endpoint = ("endpoint", row.get("endpoint_id") or row["lead_id"])
            parent[root(endpoint)] = root(lead)
    for row in ordered:
        at, available = timestamp(row.get("at")), timestamp(row.get("label_available_at"))
        if not at or not available or not at <= available <= as_of or row.get("conversion") not in (0, 1):
            continue
        if row.get("window_closed") is False:
            continue
        # Earliest eligible call wins for both lead and shared endpoint, before looking at outcomes.
        lead = row.get("lead_id")
        group = root(("lead", lead))
        if not lead or group in seen:
            continue
        keys = set()
        for item in active_observations(row.get("observations", [])[:12], as_of, require_review=False):
            observed = timestamp(item.get("observed_at")) or at
            if not at <= observed < available:
                continue
            pair = []
            for field in ("concept", "approach"):
                text = item.get(field) or ""
                canonical = normalized(text)
                canonical = aliases.get((field, canonical), canonical)
                names.setdefault((field, canonical), text.strip())
                pair.append(canonical)
            if pair[0]:
                keys.add(tuple(pair))
        if keys:
            seen.add(group)
            eligible.append({**row, "keys": keys, "available": available, "timestamp": at})
    times = sorted({row["timestamp"] for row in eligible})
    cutoff = times[len(times) // 2] if len(times) > 1 else None
    reviewed_at = timestamp(vocabulary.get("reviewed_at"))
    if cutoff and reviewed_at:
        cutoff = max(cutoff, reviewed_at)
    discovery = [r for r in eligible if cutoff and r["timestamp"] < cutoff and r["available"] < cutoff]
    validation = [r for r in eligible if cutoff and r["timestamp"] >= cutoff]
    counts = Counter(key for row in eligible for key in row["keys"])
    discovered = Counter(key for row in discovery for key in row["keys"])
    candidates = sorted((k for k, n in counts.items() if n >= min_support), key=lambda k: (-counts[k], k))[
        :200
    ]
    multiplicity = max(1, sum(n >= min_support for n in discovered.values()))
    patterns = []
    for key in candidates:
        group = [r for r in eligible if key in r["keys"]]
        conversions = sum(r["conversion"] for r in group)
        low, high = wilson(conversions, len(group))
        early = _comparison(discovery, key, min_support=min_support, multiplicity=multiplicity)
        late = _comparison(validation, key, min_support=min_support, multiplicity=multiplicity)
        supported = (
            bool(key[1])
            and discovered[key] >= min_support
            and all(
                period["difference_low"] is not None and period["difference_low"] > 0
                for period in (early, late)
            )
        )
        patterns.append(
            {
                "concept_key": key[0],
                "concept": names[("concept", key[0])],
                "approach_key": key[1],
                "approach": names[("approach", key[1])] or None,
                "samples": len(group),
                "conversions": conversions,
                "conversion_rate": conversions / len(group),
                "conversion_low": low,
                "conversion_high": high,
                "discovery": early,
                "validation": late,
                "status": "supported_correlation" if supported else "descriptive_only",
                "reason": "Supported in a later comparable period; unmeasured confounding remains."
                if supported
                else "Needs sufficient comparable groups and a later, label-embargoed comparison.",
                "automatic_policy_change": False,
            }
        )
    return {
        "status": "ready",
        "as_of": as_of.isoformat(),
        "stale": False,
        "evidence_type": "observational_correlation",
        "unit": "one_first_eligible_conversation_per_lead_or_endpoint",
        "vocabulary_version": vocabulary.get("version", 1),
        "patterns": patterns,
        "samples": len(eligible),
        "input_rows": len(ordered),
        "truncated": len(rows) > len(ordered),
        "discovery_samples": len(discovery),
        "validation_samples": len(validation),
        "validation_starts_at": cutoff.isoformat() if cutoff else None,
        "min_support": min_support,
        "candidate_limit": 200,
        "multiple_comparison": "bonferroni_simultaneous_intervals",
        "mix_adjustment": ["segment", "requested_contact", "prior_attempt_bucket"],
        "limitations": "Descriptive association, not causal lift. Approximate intervals; no adjustment for unmeasured account/rep differences. Repeated report views are not independent experiments.",
        "automatic_policy_change": False,
    }


def publish_observations(note, items, segments, *, now, call_id, review_id):
    sources = {s["segment_id"]: s for s in segments}
    previous = {i["id"]: dict(i) for i in note.data.get("observations", [])}
    published = set()
    for item in items:
        ids = sorted(set(item["evidence"]))
        identity = hashlib.sha256((call_id + ":" + ":".join(ids)).encode()).hexdigest()[:32]
        published.add(identity)
        old = previous.get(identity, {})
        if old.get("reviewer_id"):
            continue
        values = {
            "id": identity,
            "concept": item["concept"],
            "approach": item.get("approach"),
            "evidence": ids,
            "source_revisions": {i: sources[i].get("revision", 1) for i in ids},
            "status": "active",
            "review_status": "model_reviewed",
            "review_request_id": review_id,
        }
        same = all(
            old.get(key) == values[key] for key in ("concept", "approach", "source_revisions", "status")
        )
        previous[identity] = {
            **values,
            "version": old.get("version", 0) + (0 if same else 1),
            "observed_at": old["observed_at"] if same else now.isoformat(),
        }
    for identity, old in previous.items():
        if (
            identity not in published
            and old.get("status") == "active"
            and not old.get("reviewer_id")
            and set(old.get("evidence", [])) <= set(sources)
        ):
            old.update(status="retracted", version=old["version"] + 1, changed_at=now.isoformat())
    return list(previous.values())[-24:]


def invalidate_report(service, s, p, campaign_id):
    from app.db import update

    campaign = service.required(s, "campaigns", campaign_id, p)
    cached = campaign.data.get("conversation_report")
    update(
        campaign,
        conversation_revision=campaign.data.get("conversation_revision", 0) + 1,
        **({"conversation_report": {**cached, "stale": True}} if cached else {}),
    )


def guidance_context(campaign, config, *, now):
    cached = campaign.data.get("conversation_report", {})
    vocabulary = campaign.data.get("conversation_vocabulary", {"version": 1})
    at = timestamp(cached.get("as_of"))
    valid = (
        not cached.get("stale")
        and at
        and 0 <= (now - at).total_seconds() <= 86400
        and cached.get("goal") == config["conversion_goal"]
        and cached.get("window_days") == config["window_days"]
        and cached.get("vocabulary_version") == vocabulary["version"]
    )
    return {
        "evidence_type": "observational_correlation",
        "as_of": cached.get("as_of"),
        "patterns": [p for p in cached.get("patterns", []) if p.get("status") == "supported_correlation"][:5]
        if valid
        else [],
        "instruction": "Possible response approaches to consider only when relevant. These correlations do not prove causation or establish facts about this prospect. Never change calling permissions, cadence or claims.",
    }
