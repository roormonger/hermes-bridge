"""Hermes analytics helpers.

Reads per-model usage statistics directly from the Hermes session database
when hermes_state is available (typical when running as a Hermes plugin).
Falls back to HTTP proxy when the package is not importable.
"""

from __future__ import annotations

import time
from typing import Any, Optional


def get_models_analytics(days: int = 30, profile: Optional[str] = None) -> dict[str, Any]:
    """Return the same payload as Hermes dashboard /api/analytics/models.

    Tries to read directly from the Hermes session DB to avoid auth/cookie
    issues. If hermes_state is unavailable, returns an empty models list so the
    caller can fall back to the HTTP proxy.
    """
    try:
        from hermes_state import SessionDB
    except Exception as exc:
        raise RuntimeError(f"hermes_state is not available: {exc}")

    if profile:
        # Hermes Desktop supports named profiles; default bridge usage has no
        # profile and reads the main HERMES_HOME state database.
        try:
            from hermes_cli.web_server import _open_session_db_for_profile
            db = _open_session_db_for_profile(profile)
        except Exception as exc:
            raise RuntimeError(f"Cannot open session DB for profile {profile}: {exc}")
    else:
        db = SessionDB()

    try:
        cutoff = time.time() - (days * 86400)

        cur = db._conn.execute(
            """
            SELECT model,
                   billing_provider,
                   SUM(input_tokens) as input_tokens,
                   SUM(output_tokens) as output_tokens,
                   SUM(cache_read_tokens) as cache_read_tokens,
                   SUM(reasoning_tokens) as reasoning_tokens,
                   COALESCE(SUM(estimated_cost_usd), 0) as estimated_cost,
                   COALESCE(SUM(actual_cost_usd), 0) as actual_cost,
                   COUNT(*) as sessions,
                   SUM(COALESCE(api_call_count, 0)) as api_calls,
                   SUM(tool_call_count) as tool_calls,
                   MAX(started_at) as last_used_at,
                   AVG(input_tokens + output_tokens) as avg_tokens_per_session
            FROM sessions WHERE started_at > ? AND model IS NOT NULL AND model != ''
            GROUP BY model, billing_provider
            ORDER BY SUM(input_tokens) + SUM(output_tokens) DESC
            """,
            (cutoff,),
        )
        raw_rows = [dict(r) for r in cur.fetchall()]

        # Fold session-only rows into the single accounted provider row when
        # ownership is unambiguous. Matches Hermes dashboard logic.
        rows_by_model: dict[str, list[dict[str, Any]]] = {}
        for row in raw_rows:
            rows_by_model.setdefault(row.get("model") or "", []).append(row)

        rows: list[dict[str, Any]] = []
        usage_keys = (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "reasoning_tokens",
            "estimated_cost",
            "actual_cost",
            "api_calls",
            "tool_calls",
        )
        for model_rows in rows_by_model.values():
            provider_rows = [r for r in model_rows if r.get("billing_provider")]
            if len(provider_rows) == 1:
                target = provider_rows[0]
                for row in model_rows:
                    if row is target or row.get("billing_provider"):
                        continue
                    has_usage = any((row.get(key) or 0) != 0 for key in usage_keys)
                    if has_usage:
                        continue
                    target["sessions"] = (target.get("sessions") or 0) + (row.get("sessions") or 0)
                    target["last_used_at"] = max(
                        target.get("last_used_at") or 0, row.get("last_used_at") or 0
                    )
                    total_tokens = (target.get("input_tokens") or 0) + (target.get("output_tokens") or 0)
                    sessions = target.get("sessions") or 0
                    target["avg_tokens_per_session"] = total_tokens / sessions if sessions else 0
                rows.append(target)
                rows.extend(
                    r
                    for r in model_rows
                    if r is not target
                    and (
                        r.get("billing_provider")
                        or any((r.get(key) or 0) != 0 for key in usage_keys)
                    )
                )
            else:
                rows.extend(model_rows)

        rows.sort(
            key=lambda r: (r.get("input_tokens") or 0) + (r.get("output_tokens") or 0),
            reverse=True,
        )

        models = []
        for row in rows:
            provider = row.get("billing_provider") or ""
            model_name = row["model"]
            caps: dict[str, Any] = {}
            try:
                from agent.models_dev import get_model_capabilities

                mc = get_model_capabilities(provider=provider, model=model_name)
                if mc is not None:
                    caps = {
                        "supports_tools": mc.supports_tools,
                        "supports_vision": mc.supports_vision,
                        "supports_reasoning": mc.supports_reasoning,
                        "context_window": mc.context_window,
                        "max_output_tokens": mc.max_output_tokens,
                        "model_family": mc.model_family,
                    }
            except Exception:
                pass

            models.append(
                {
                    "model": model_name,
                    "provider": provider,
                    "input_tokens": row["input_tokens"],
                    "output_tokens": row["output_tokens"],
                    "cache_read_tokens": row["cache_read_tokens"],
                    "reasoning_tokens": row["reasoning_tokens"],
                    "estimated_cost": row["estimated_cost"],
                    "actual_cost": row["actual_cost"],
                    "sessions": row["sessions"],
                    "api_calls": row["api_calls"],
                    "tool_calls": row["tool_calls"],
                    "last_used_at": row["last_used_at"],
                    "avg_tokens_per_session": row["avg_tokens_per_session"],
                    "capabilities": caps,
                }
            )

        totals_cur = db._conn.execute(
            """
            SELECT COUNT(DISTINCT model) as distinct_models,
                   SUM(input_tokens) as total_input,
                   SUM(output_tokens) as total_output,
                   SUM(cache_read_tokens) as total_cache_read,
                   SUM(reasoning_tokens) as total_reasoning,
                   COALESCE(SUM(estimated_cost_usd), 0) as total_estimated_cost,
                   COALESCE(SUM(actual_cost_usd), 0) as total_actual_cost,
                   COUNT(*) as total_sessions,
                   SUM(COALESCE(api_call_count, 0)) as total_api_calls
            FROM sessions WHERE started_at > ? AND model IS NOT NULL AND model != ''
            """,
            (cutoff,),
        )
        totals = dict(totals_cur.fetchone())

        return {
            "models": models,
            "totals": totals,
            "period_days": days,
        }
    finally:
        db.close()
