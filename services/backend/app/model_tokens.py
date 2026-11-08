"""Model counters share the usage ledger with costs; subscription work still consumes tokens."""

from sqlalchemy import and_, case, exists, func, or_, select

from app.lead_lists import in_list
from app.platform.db import MODELS, emit, update
from app.privacy import usage_metadata
from app.providers.inference import InferenceResult


def counters(value):
    value = usage_metadata(value)
    result = dict(value)
    for target, names in {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "output_tokens": ("output_tokens", "completion_tokens"),
    }.items():
        found = next((value[k] for k in names if k in value), None)
        if found is not None:
            result[target] = int(found)
    for key, details, alt in (
        ("cached_tokens", "input_tokens_details", "prompt_tokens_details"),
        ("reasoning_tokens", "output_tokens_details", "completion_tokens_details"),
    ):
        amount = value.get(key, value.get(details, {}).get(key, value.get(alt, {}).get(key)))
        if amount is not None:
            result[key] = int(amount)
    return result


def capture(service, p, provider, kind, request_key, context, value, status="running"):
    """A cumulative update replaces its prior counters, including on failure/cancellation."""
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        prior = service._request_hold(s, p, request_key)
        previous = (prior.data.get("usage") or {}) if prior else {}
        merged = {**previous, **counters(value)}
        for key in ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens", "web_search_calls"):
            if key in previous and key in merged:
                merged[key] = max(previous[key], merged[key])
        lead_ids = context.get("lead_ids") or []
        settlement = (
            merged
            if status != "running"
            else {k: v for k, v in merged.items() if k not in {"cost", "cost_usd"}}
        )
        identity = service.usage(
            p,
            provider,
            kind,
            InferenceResult({}, settlement),
            object_id=lead_ids[0] if lead_ids else context.get("object_id"),
            request_key=request_key,
            reserved=0,
            session=s,
        )
        row = service.required(s, "usage_ledger", identity, p)
        known = "input_tokens" in merged and "output_tokens" in merged
        terminal = row.data.get("run_status") in {"completed", "failed", "cancelled"}
        if terminal and status == "running":
            status = row.data["run_status"]
        update(
            row,
            usage=merged,
            model_context=context,
            run_status=status,
            token_status=("partial" if status in {"failed", "cancelled"} else "reported")
            if known
            else "unknown",
            user_id=p.get("user_id"),
        )
        emit(s, p["tenant_id"], "usage.updated", row, service.clock())


def _array_has(service, expression, value):
    values = (func.json_each(expression)).table_valued("value")
    return exists(select(1).select_from(values).where(values.c.value == value))


def report(
    service, p, *, campaign_id=None, list_name=None, base_version=None, kind=None, lead_id=None, limit=100
):
    model, calls, leads = (MODELS[k] for k in ("usage_ledger", "call_attempts", "leads"))
    data, context = model.data, model.data["model_context"]
    provider = data["provider"].as_string()
    work_kind = func.coalesce(data["work_kind"].as_string(), data["kind"].as_string())
    object_id = data["object_id"].as_string()
    query = (
        select(model)
        .outerjoin(
            calls,
            and_(
                calls.id == func.coalesce(model.op_call_id, object_id),
                calls.tenant_id == p["tenant_id"],
                calls.seller_id == p["seller_id"],
            ),
        )
        .outerjoin(
            leads,
            and_(
                leads.id == func.coalesce(calls.op_lead_id, object_id),
                leads.tenant_id == p["tenant_id"],
                leads.seller_id == p["seller_id"],
            ),
        )
        .where(
            model.tenant_id == p["tenant_id"],
            model.seller_id == p["seller_id"],
            provider.in_(["openrouter", "openai", "chatgpt_plan", "codex", "litellm", "simulation"]),
        )
    )
    if campaign_id:
        query = query.where(
            func.coalesce(context["campaign_id"].as_string(), calls.op_campaign_id, leads.op_campaign_id)
            == campaign_id
        )
    if list_name is not None:
        query = query.where(
            or_(
                _array_has(service, context["list_names"], list_name),
                in_list(leads, list_name),
            )
        )
    if base_version is not None:
        query = query.where(context["base_version"].as_integer() == base_version)
    if kind:
        query = query.where(work_kind == kind)
    if lead_id:
        query = query.where(or_(_array_has(service, context["lead_ids"], lead_id), leads.id == lead_id))
    usage = data["usage"]
    input_count = func.coalesce(usage["input_tokens"].as_integer(), usage["prompt_tokens"].as_integer())
    output_count = func.coalesce(usage["output_tokens"].as_integer(), usage["completion_tokens"].as_integer())
    known = and_(input_count.is_not(None), output_count.is_not(None))
    metrics = [
        func.count().label("requests"),
        func.sum(case((known, 0), else_=1)).label("unknown_requests"),
        func.sum(case((data["token_status"].as_string() == "partial", 1), else_=0)).label("partial_requests"),
        func.sum(case((data["run_status"].as_string() == "running", 1), else_=0)).label("running"),
    ]
    for name, expression in (
        ("input_tokens", input_count),
        ("output_tokens", output_count),
        (
            "cached_tokens",
            func.coalesce(
                usage["cached_tokens"].as_integer(),
                usage["input_tokens_details"]["cached_tokens"].as_integer(),
                usage["prompt_tokens_details"]["cached_tokens"].as_integer(),
            ),
        ),
        (
            "reasoning_tokens",
            func.coalesce(
                usage["reasoning_tokens"].as_integer(),
                usage["output_tokens_details"]["reasoning_tokens"].as_integer(),
                usage["completion_tokens_details"]["reasoning_tokens"].as_integer(),
            ),
        ),
    ):
        metrics.append(func.coalesce(func.sum(expression), 0).label(name))
        metrics.append(func.sum(case((expression.is_not(None), 1), else_=0)).label(name + "_known_requests"))

    def totals(row):
        value = {k: int(v or 0) for k, v in row.items()}
        for name in ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens"):
            if value["requests"] and not value.pop(name + "_known_requests"):
                value[name] = None
            else:
                value.pop(name + "_known_requests", None)
        value["total_tokens"] = (
            value["input_tokens"] + value["output_tokens"]
            if value["input_tokens"] is not None and value["output_tokens"] is not None
            else None
        )
        return value

    with service.db.transaction(False) as s:
        total = totals(
            s.execute(query.with_only_columns(*metrics, maintain_column_froms=True)).mappings().one()
        )
        groups = []
        for row in s.execute(
            query.with_only_columns(
                provider.label("provider"), work_kind.label("kind"), *metrics, maintain_column_froms=True
            ).group_by(provider, work_kind)
        ).mappings():
            value = dict(row)
            groups.append({"provider": value.pop("provider"), "kind": value.pop("kind"), **totals(value)})
        records = []
        for row in s.scalars(query.order_by(model.created_at.desc(), model.id.desc()).limit(limit)):
            d, u = row.data, counters(row.data.get("usage") or {})
            ctx = d.get("model_context") or {}
            count = len(ctx.get("lead_ids") or [])
            records.append(
                {
                    "id": row.id,
                    "provider": d.get("provider"),
                    "kind": d.get("work_kind", d.get("kind")),
                    "model": ctx.get("model"),
                    "at": d.get("at") or row.created_at.isoformat(),
                    "context": ctx,
                    "status": d.get("run_status", "completed"),
                    "token_status": d.get(
                        "token_status",
                        "reported" if "input_tokens" in u and "output_tokens" in u else "unknown",
                    ),
                    "input_tokens": u.get("input_tokens"),
                    "output_tokens": u.get("output_tokens"),
                    "cached_tokens": u.get("cached_tokens"),
                    "reasoning_tokens": u.get("reasoning_tokens"),
                    "billing": "subscription_included"
                    if d.get("estimate_source") == "chatgpt_plan_included"
                    else "provider_receipt"
                    if d.get("billing_verified")
                    else "unpriced",
                    "cost_usd": d.get("cost_usd"),
                    "attribution": "shared_thread" if count > 1 else "request",
                    "per_lead_estimated_tokens": (u.get("input_tokens", 0) + u.get("output_tokens", 0))
                    / count
                    if count > 1 and "input_tokens" in u
                    else None,
                }
            )
        return {
            "totals": total,
            "groups": groups,
            "records": records,
            "truncated": total["requests"] > len(records),
            "shared_attribution": bool(lead_id),
            "historical_preparation_usage": "not recorded before 0.10.0; absent usage is unknown",
        }


def estimate(service, p, *, list_name=None):
    """Measured request averages are estimates for future work, never invented exact lead charges."""
    from statistics import median

    with service.db.transaction(False) as s:
        campaign = service.selected_campaign(s, p)
        base = service.script_base(s, p, campaign)
        model = MODELS["leads"]
        query = select(model).where(
            model.tenant_id == p["tenant_id"],
            model.seller_id == p["seller_id"],
            model.op_campaign_id == campaign.id,
            model.op_status.in_(["eligible", "preparing"]),
        )
        if list_name is not None:
            query = query.where(in_list(model, list_name))
        eligible = [r for r in s.scalars(query) if service.preparation_permitted(s, p, r)]
        count = len(eligible)
        remaining = sum(service.preparation_state(s, p, r)["status"] != "ready" for r in eligible)
    receipt = report(
        service,
        p,
        campaign_id=campaign.id,
        list_name=list_name,
        base_version=base["version"],
    )
    samples = [
        r
        for r in receipt["records"]
        if r["kind"] in {"preparation", "script_review"}
        and r["status"] == "completed"
        and r["token_status"] == "reported"  # noqa: S105 - metering state, not an authentication token
        and r["context"].get("lead_ids")
    ]
    rates = {
        kind: [
            (r["input_tokens"] + r["output_tokens"]) / len(r["context"]["lead_ids"])
            for r in samples
            if r["kind"] == kind
        ]
        for kind in ("preparation", "script_review")
    }
    sampled = {i for r in samples if r["kind"] == "preparation" for i in r["context"]["lead_ids"]}
    rate = sum(median(values) for values in rates.values() if values) if rates["preparation"] else None
    return {
        "basis": "measured" if rate is not None else "unavailable",
        "leads": count,
        "remaining_leads": remaining,
        "measured_leads": len(sampled),
        "tokens_per_lead_estimate": rate,
        "estimated_total_tokens": round(rate * count) if rate is not None else None,
        "estimated_remaining_tokens": round(rate * remaining) if rate is not None else None,
        "base_version": base["version"],
        "note": "Estimate uses up to 100 recent reported writing/research and review receipts for this master. Shared requests are allocated estimates per lead; future searches, retries and reviews can change actual usage.",
    }
