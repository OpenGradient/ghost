#!/usr/bin/env python3
"""Chat-app (Supabase) auth for ghost's hosted path.

Stores the bundle handed back by the website's `/cli-auth` page -- the Supabase
session tokens plus the public client config (chat-api URL + TEE registry coords)
-- and keeps the short-lived access token fresh by exchanging the refresh token
against Supabase's GoTrue token endpoint.

Both the login helper and the OHTTP bridge import this so there is one source of
truth for "what's my current bearer token, and where do I send it".
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, Optional

import httpx

AUTH_FILE = os.path.expanduser("~/.ghost/privacy/chat_auth.json")
# Refresh a little early so an in-flight request never races the expiry.
_REFRESH_SKEW_SECONDS = 120
_lock = threading.Lock()


def load_auth() -> Optional[Dict[str, Any]]:
    """Return the stored auth bundle, or None if not logged in."""
    try:
        with open(AUTH_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_auth(bundle: Dict[str, Any]) -> None:
    """Persist the auth bundle (0600) to the privacy dir."""
    os.makedirs(os.path.dirname(AUTH_FILE), exist_ok=True)
    fd = os.open(AUTH_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(bundle, f, indent=2)


def is_logged_in() -> bool:
    bundle = load_auth()
    return bool(bundle and bundle.get("access_token"))


def get_config() -> Dict[str, Any]:
    """Return the public client config captured at login.

    Environment overrides (GHOST_CHAT_API_BASE_URL, GHOST_TEE_REGISTRY_RPC_URL,
    GHOST_TEE_REGISTRY_ADDRESS, GHOST_TEE_REGISTRY_TEE_TYPE, GHOST_APP_ENV) win
    over the stored values, for self-hosted / staging setups.
    """
    bundle = load_auth() or {}
    config = dict(bundle.get("config") or {})

    def override(key: str, env: str, cast=lambda x: x):
        val = os.environ.get(env)
        if val:
            config[key] = cast(val)

    override("chat_api_base_url", "GHOST_CHAT_API_BASE_URL")
    override("tee_registry_rpc_url", "GHOST_TEE_REGISTRY_RPC_URL")
    override("tee_registry_address", "GHOST_TEE_REGISTRY_ADDRESS")
    override("tee_registry_tee_type", "GHOST_TEE_REGISTRY_TEE_TYPE", int)
    override("app_env", "GHOST_APP_ENV")
    return config


def _needs_refresh(bundle: Dict[str, Any]) -> bool:
    expires_at = bundle.get("expires_at")
    if not expires_at:
        return False  # no expiry info -> use as-is, refresh on 401
    return time.time() >= float(expires_at) - _REFRESH_SKEW_SECONDS


def _refresh(bundle: Dict[str, Any]) -> Dict[str, Any]:
    """Exchange the refresh token for a fresh Supabase session."""
    config = bundle.get("config") or {}
    supabase_url = config.get("supabase_url")
    anon_key = config.get("supabase_anon_key")
    refresh_token = bundle.get("refresh_token")
    if not (supabase_url and anon_key and refresh_token):
        raise RuntimeError("Cannot refresh: missing supabase config or refresh token")

    resp = httpx.post(
        f"{supabase_url.rstrip('/')}/auth/v1/token",
        params={"grant_type": "refresh_token"},
        headers={"apikey": anon_key, "Content-Type": "application/json"},
        json={"refresh_token": refresh_token},
        timeout=20.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Supabase token refresh failed ({resp.status_code}): {resp.text[:200]}")
    data = resp.json()
    bundle = dict(bundle)
    bundle["access_token"] = data["access_token"]
    bundle["refresh_token"] = data.get("refresh_token", refresh_token)
    bundle["token_type"] = data.get("token_type", bundle.get("token_type", "bearer"))
    bundle["expires_in"] = data.get("expires_in")
    bundle["expires_at"] = data.get("expires_at")
    return bundle


def get_valid_access_token(force_refresh: bool = False) -> str:
    """Return a currently-valid bearer token, refreshing it if needed.

    Raises RuntimeError if not logged in or the refresh fails.
    """
    with _lock:
        bundle = load_auth()
        if not bundle or not bundle.get("access_token"):
            raise RuntimeError("Not logged in. Run `ghost-login` to connect your account.")
        if force_refresh or _needs_refresh(bundle):
            bundle = _refresh(bundle)
            save_auth(bundle)
        return bundle["access_token"]
