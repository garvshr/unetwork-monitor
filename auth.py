"""Refresh-token based authentication for the U Network API.

A manual login provides the initial refresh token. This module exchanges it for
short-lived access tokens and persists every newly issued refresh token,
because the API rotates it on each refresh.
"""

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)


class AuthenticationError(RuntimeError):
    """Raised when credentials are missing, rejected, or cannot be refreshed."""


class AuthManager:
    """Provides always-valid access tokens and rotates stored refresh tokens."""

    def __init__(self, config, config_path, session=None):
        self._config = config
        self._config_path = config_path
        self._session = session or requests.Session()
        self._lock = threading.Lock()

        self.api_key = (
            os.environ.get("UNETWORK_API_KEY")
            or config.get("supabase_publishable_key")
            or ""
        )
        env_refresh_token = os.environ.get("UNETWORK_REFRESH_TOKEN")
        self._refresh_token_from_env = bool(env_refresh_token)
        self._refresh_token = env_refresh_token or config.get("refresh_token") or ""

        self._base_url = str(config.get("api_base_url", "https://api.unityedge.io")).rstrip("/")
        self._timeout = float(config.get("request_timeout_seconds", 30))
        self._max_attempts = int(config.get("http_max_attempts", 3))
        self._refresh_margin = int(config.get("refresh_margin_seconds", 120))

        self._access_token = config.get("access_token") or None
        expires_at = config.get("token_expires_at")
        self._expires_at = float(expires_at) if expires_at else 0.0

        if not self.api_key or str(self.api_key).startswith("PASTE_YOUR"):
            raise AuthenticationError(
                "Missing Supabase publishable key: set UNETWORK_API_KEY or fill "
                "'supabase_publishable_key' in config.json."
            )
        if not self._refresh_token or str(self._refresh_token).startswith("PASTE_YOUR"):
            raise AuthenticationError(
                "No refresh token available: log in manually once and store the "
                "refresh token in config.json (or export UNETWORK_REFRESH_TOKEN)."
            )

    def get_access_token(self):
        """Return a valid access token, refreshing it shortly before expiry."""
        with self._lock:
            if self._access_token and time.time() < self._expires_at - self._refresh_margin:
                return self._access_token
            return self._refresh()

    def force_refresh(self):
        """Force a token refresh regardless of remaining validity."""
        with self._lock:
            return self._refresh()

    def _refresh(self):
        """Exchange the current refresh token for a new access/refresh pair."""
        url = f"{self._base_url}/auth/v1/token"
        params = {"grant_type": "refresh_token"}
        headers = {"apikey": self.api_key, "Content-Type": "application/json"}
        payload = {"refresh_token": self._refresh_token}

        last_error = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._session.post(
                    url, params=params, headers=headers, json=payload, timeout=self._timeout
                )
            except requests.RequestException as exc:
                last_error = f"network error: {exc}"
                logger.warning("Token refresh %d/%d failed: %s", attempt, self._max_attempts, exc)
            else:
                if response.status_code == 200:
                    return self._on_success(response)
                if 400 <= response.status_code < 500:
                    raise AuthenticationError(
                        f"Refresh token rejected (HTTP {response.status_code}). "
                        "Log in manually again and update the stored refresh token."
                    )
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                logger.warning("Token refresh %d/%d failed: %s", attempt, self._max_attempts, last_error)
            if attempt < self._max_attempts:
                time.sleep(min(2 ** attempt, 10))

        raise ConnectionError(
            f"Token refresh failed after {self._max_attempts} attempts ({last_error})"
        )

    def _on_success(self, response):
        try:
            data = response.json()
        except ValueError as exc:
            raise AuthenticationError(f"Malformed token response: {exc}") from exc

        access_token = data.get("access_token")
        refresh_token = data.get("refresh_token")
        expires_in = int(data.get("expires_in", 3600))
        if not access_token or not refresh_token:
            raise AuthenticationError(f"Unexpected token response keys: {sorted(data)}")

        self._access_token = access_token
        self._refresh_token = refresh_token
        self._expires_at = time.time() + expires_in
        self._persist()
        logger.info("Access token refreshed (valid %ss); refresh token rotated.", expires_in)
        return access_token

    def _persist(self):
        self._config["access_token"] = self._access_token
        self._config["refresh_token"] = self._refresh_token
        self._config["token_expires_at"] = self._expires_at
        self._config["token_updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self._config_path.parent), prefix=self._config_path.name, suffix=".tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._config, handle, indent=2)
            os.replace(tmp_name, self._config_path)
        except OSError as exc:
            logger.error("Could not persist rotated tokens to %s: %s", self._config_path, exc)
        if self._refresh_token_from_env:
            logger.warning(
                "UNETWORK_REFRESH_TOKEN env var overrides config.json; the rotated "
                "token was saved to config.json but will be ignored until the env "
                "var is removed."
            )
