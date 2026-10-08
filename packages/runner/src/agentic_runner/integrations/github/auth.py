from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt

_DEFAULT_API_BASE_URL = "https://api.github.com"
_REQUEST_TIMEOUT_SECONDS = 10.0
_INSTALLATION_TOKEN_REFRESH_WINDOW = timedelta(minutes=5)


class GitHubAppAuthenticationError(RuntimeError):
    """Sanitized GitHub App authentication failure."""


class GitHubAppAuthProvider:
    """GitHub App authentication provider for app JWTs and installation tokens."""

    def __init__(
        self,
        *,
        app_id: str,
        installation_id: str,
        private_key_pem: str,
        api_base_url: str = _DEFAULT_API_BASE_URL,
        http_client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._validate_credentials(app_id, installation_id, private_key_pem)
        if http_client is not None and transport is not None:
            raise ValueError("Provide either http_client or transport, not both")

        self._app_id = app_id
        self._installation_id = installation_id
        self._private_key_pem = private_key_pem
        self._api_base_url = api_base_url.rstrip("/")
        self._http_client = http_client or httpx.Client(
            transport=transport,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        self._installation_token: str | None = None
        self._installation_token_expires_at: datetime | None = None
        self._clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _validate_credentials(app_id: str, installation_id: str, private_key_pem: str) -> None:
        missing_fields = [
            field
            for field, value in (
                ("app_id", app_id),
                ("installation_id", installation_id),
                ("private_key_pem", private_key_pem),
            )
            if value.strip() == ""
        ]
        if missing_fields:
            raise ValueError(
                "GitHub App credentials are required and must be non-empty: "
                + ", ".join(missing_fields)
            )

    def generate_app_jwt(self) -> str:
        issued_at = int(time.time()) - 60
        claims = {
            "iat": issued_at,
            "exp": issued_at + 600,
            "iss": self._app_id,
        }
        try:
            token = jwt.encode(claims, self._private_key_pem, algorithm="RS256")
        except Exception:
            raise GitHubAppAuthenticationError("GitHub App JWT generation failed") from None
        return token

    def get_installation_token(self) -> str:
        if self._cached_installation_token_is_reusable():
            assert self._installation_token is not None
            return self._installation_token

        app_jwt = self.generate_app_jwt()
        response_data = self._send_json_request(
            "POST",
            f"/app/installations/{self._installation_id}/access_tokens",
            headers=self.github_headers(f"Bearer {app_jwt}"),
        )
        token = response_data.get("token")
        if not isinstance(token, str) or token.strip() == "":
            raise GitHubAppAuthenticationError(
                "GitHub App installation token response was invalid"
            ) from None
        expires_at = self._parse_installation_token_expires_at(response_data)
        self._installation_token = token
        self._installation_token_expires_at = expires_at
        return token

    def _cached_installation_token_is_reusable(self) -> bool:
        if self._installation_token is None or self._installation_token_expires_at is None:
            return False
        refresh_deadline = self._clock() + _INSTALLATION_TOKEN_REFRESH_WINDOW
        return self._installation_token_expires_at > refresh_deadline

    @staticmethod
    def _parse_installation_token_expires_at(response_data: dict[str, Any]) -> datetime:
        expires_at_value = response_data.get("expires_at")
        if not isinstance(expires_at_value, str) or expires_at_value.strip() == "":
            raise GitHubAppAuthenticationError(
                "GitHub App installation token response was invalid"
            ) from None

        try:
            parsed_expires_at = datetime.fromisoformat(expires_at_value.replace("Z", "+00:00"))
        except ValueError:
            raise GitHubAppAuthenticationError(
                "GitHub App installation token response was invalid"
            ) from None

        if parsed_expires_at.tzinfo is None:
            raise GitHubAppAuthenticationError(
                "GitHub App installation token response was invalid"
            ) from None
        return parsed_expires_at.astimezone(UTC)

    @staticmethod
    def github_headers(authorization: str) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": authorization,
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _send_json_request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._api_base_url}/{path.lstrip('/')}"
        try:
            response = self._http_client.request(
                method,
                url,
                headers=headers,
                json=json_body,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError:
            raise GitHubAppAuthenticationError("GitHub App authentication request failed") from None

        if response.status_code < 200 or response.status_code >= 300:
            raise GitHubAppAuthenticationError(
                f"GitHub App authentication request failed with status {response.status_code}"
            ) from None

        try:
            response_data = response.json()
        except ValueError:
            raise GitHubAppAuthenticationError(
                "GitHub App authentication response was not valid JSON"
            ) from None
        if not isinstance(response_data, dict):
            raise GitHubAppAuthenticationError(
                "GitHub App authentication response JSON object was invalid"
            ) from None
        return response_data
