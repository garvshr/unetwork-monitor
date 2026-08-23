"""Client for the U Network licenses endpoint."""

import logging
import time

import requests

logger = logging.getLogger(__name__)


class ApiError(RuntimeError):
    def __init__(self, status_code, message):
        super().__init__(f"U Network API returned HTTP {status_code}: {message}")
        self.status_code = status_code


class UNetworkClient:
    """Calls /functions/v1/licenses_get_licenses with automatic pagination."""

    def __init__(self, auth_manager, config, session=None):
        self._auth = auth_manager
        self._session = session or requests.Session()
        self._base_url = str(config.get("api_base_url", "https://api.unityedge.io")).rstrip("/")
        self._role = config.get("role", "ulo")
        self._page_size = int(config.get("page_size", 20))
        self._enabled_only = bool(config.get("enabled", True))
        self._timeout = float(config.get("request_timeout_seconds", 30))
        self._max_attempts = int(config.get("http_max_attempts", 3))
        self._max_pages = int(config.get("max_pages_per_poll", 100))

    def get_licenses(self, page=1):
        url = f"{self._base_url}/functions/v1/licenses_get_licenses"
        headers = {
            "Authorization": f"Bearer {self._auth.get_access_token()}",
            "apikey": self._auth.api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "role": self._role,
            "page": page,
            "pageSize": self._page_size,
            "enabled": self._enabled_only,
            "skip": (page - 1) * self._page_size,
            "take": self._page_size,
        }

        last_error = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._session.post(url, headers=headers, json=payload, timeout=self._timeout)
            except requests.RequestException as exc:
                last_error = f"network error: {exc}"
                logger.warning("licenses_get_licenses %d/%d failed: %s", attempt, self._max_attempts, exc)
            else:
                if response.status_code == 200:
                    return self._parse_items(response)
                if response.status_code == 401 and attempt < self._max_attempts:
                    logger.info("Got 401 from licenses endpoint; forcing token refresh and retrying.")
                    headers["Authorization"] = f"Bearer {self._auth.force_refresh()}"
                    continue
                raise ApiError(response.status_code, response.text[:200])
            if attempt < self._max_attempts:
                time.sleep(min(2 ** attempt, 10))

        raise ConnectionError(
            f"licenses_get_licenses failed after {self._max_attempts} attempts ({last_error})"
        )

    def iter_all_licenses(self):
        """Yield every license, following pages using the embedded totalCount."""
        page = 1
        fetched = 0
        total_count = None
        while page <= self._max_pages:
            items = self.get_licenses(page=page)
            if not items:
                break
            fetched += len(items)
            if total_count is None:
                raw_total = items[0].get("totalCount")
                total_count = int(raw_total) if raw_total is not None else None
            for item in items:
                yield item
            if total_count is not None and fetched >= total_count:
                break
            if len(items) < self._page_size:
                break
            page += 1
        if page > self._max_pages:
            logger.warning("Reached max page limit (%d) while fetching licenses.", self._max_pages)

    @staticmethod
    def _parse_items(response):
        try:
            data = response.json()
        except ValueError as exc:
            raise ApiError(200, f"malformed JSON response: {exc}") from exc
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("data", "items", "results", "licenses"):
                if isinstance(data.get(key), list):
                    return data[key]
        raise ApiError(200, f"unexpected response shape: {type(data).__name__}")
