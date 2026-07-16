"""Helpers for free-model-only user access to the model catalog."""

from __future__ import annotations

from typing import Any, Optional


FREE_SUFFIX = ":free"


def _norm_id(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("id") or value.get("model") or value)
    return str(value)


def profile_display_name(model_id: str) -> str:
    """Last path segment — same convention as the chat UI profile label."""
    return model_id.split("/")[-1] if model_id else ""


def has_free_suffix(model_id: str) -> bool:
    """True when the model id or its display name ends with ``:free`` (case-insensitive)."""
    mid = (model_id or "").strip()
    if not mid:
        return False
    if mid.lower().endswith(FREE_SUFFIX):
        return True
    return profile_display_name(mid).lower().endswith(FREE_SUFFIX)


def _pricing_entry(provider: dict, model_id: str) -> Optional[dict]:
    pricing = provider.get("pricing") or {}
    if not isinstance(pricing, dict):
        return None
    entry = pricing.get(model_id)
    return entry if isinstance(entry, dict) else None


def is_catalog_model_free(provider: dict, model: Any) -> bool:
    """True if the catalog model is free via pricing flag or ``:free`` suffix."""
    model_id = _norm_id(model)
    if has_free_suffix(model_id):
        return True
    entry = _pricing_entry(provider, model_id)
    if entry and entry.get("free") is True:
        return True
    return False


def is_profile_allowed_for_free_user(model_id: str) -> bool:
    """Profiles are hidden for free-only users unless the name ends with ``:free``."""
    return has_free_suffix(model_id)


def filter_model_options_for_free_user(payload: dict) -> dict:
    """Return a copy of ``model.options`` with only free catalog models."""
    providers_in = payload.get("providers") or []
    providers_out = []
    for provider in providers_in:
        if not isinstance(provider, dict):
            continue
        models_in = provider.get("models") or []
        kept_models = [m for m in models_in if is_catalog_model_free(provider, m)]
        if not kept_models and not (provider.get("unavailable_models") or []):
            # Drop empty providers to keep the picker clean.
            continue
        kept_ids = {_norm_id(m) for m in kept_models}
        pricing_in = provider.get("pricing") if isinstance(provider.get("pricing"), dict) else {}
        pricing_out = {k: v for k, v in pricing_in.items() if k in kept_ids}
        unavailable = [
            m for m in (provider.get("unavailable_models") or [])
            if _norm_id(m) in kept_ids
        ]
        row = dict(provider)
        row["models"] = kept_models
        if pricing_in:
            row["pricing"] = pricing_out
        if "unavailable_models" in provider:
            row["unavailable_models"] = unavailable
        providers_out.append(row)
    out = dict(payload)
    out["providers"] = providers_out
    return out


def filter_analytics_models_for_free_user(payload: dict) -> dict:
    """Keep only analytics/profile rows whose model name ends with ``:free``."""
    models_in = payload.get("models") or []
    models_out = [
        m for m in models_in
        if isinstance(m, dict) and is_profile_allowed_for_free_user(str(m.get("model") or ""))
    ]
    out = dict(payload)
    out["models"] = models_out
    return out


def model_allowed_for_free_user(model_id: str, provider_slug: str, catalog: dict) -> bool:
    """Server-side gate for ``POST /v1/model`` when the user is free-only."""
    if has_free_suffix(model_id):
        return True
    for provider in catalog.get("providers") or []:
        if not isinstance(provider, dict):
            continue
        slug = provider.get("slug") or provider.get("id") or ""
        if provider_slug and slug and slug != provider_slug:
            continue
        for m in provider.get("models") or []:
            if _norm_id(m) != model_id:
                continue
            return is_catalog_model_free(provider, m)
        # Model may still appear only in pricing map
        if is_catalog_model_free(provider, model_id):
            return True
    return False
