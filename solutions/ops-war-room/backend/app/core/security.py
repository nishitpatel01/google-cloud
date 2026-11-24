"""Security middleware: strict CSP, rate limiting, ES256 Cloud IAP JWT, and Model Armor REST v1."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Literal

import httpx
import jwt
from jwt import PyJWKClient
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import Settings

STRICT_CSP_HEADER = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'"
)

IAP_JWKS_URL = "https://www.gstatic.com/iap/verify/public_key-jwk"
IAP_ISSUER = "https://cloud.google.com/iap"


class PromptBlockedError(PermissionError):
    """Raised when Google Cloud Model Armor flags an input prompt or log payload."""


@dataclass(frozen=True)
class ModelArmorVerdict:
    checked: bool
    blocked: bool
    match_state: str
    template_id: str = ""
    detail: str = ""


def _get_adc_token() -> str:
    import google.auth
    from google.auth.transport.requests import Request as AuthRequest

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    if not creds.valid:
        creds.refresh(AuthRequest())
    if not creds.token:
        raise RuntimeError("Failed to obtain ADC OAuth2 token for Model Armor")
    return str(creds.token)


def screen_with_model_armor(
    *,
    settings: Settings,
    text: str,
    mode: Literal["user_prompt", "model_response"] = "user_prompt",
    raise_on_block: bool = True,
) -> ModelArmorVerdict:
    """Screen text against dedicated Google Cloud Model Armor REST v1 template."""
    tpl = settings.model_armor_template_id.strip()
    project = settings.gcp_project_id.strip()
    if not tpl or not project:
        return ModelArmorVerdict(
            checked=False,
            blocked=False,
            match_state="NOT_CONFIGURED",
            detail="MODEL_ARMOR_TEMPLATE_ID or GCP_PROJECT_ID unset",
        )

    loc = settings.model_armor_location.strip() or "us-central1"
    action = "sanitizeUserPrompt" if mode == "user_prompt" else "sanitizeModelResponse"
    body_key = "userPromptData" if mode == "user_prompt" else "modelResponseData"
    url = (
        f"https://modelarmor.{loc}.rep.googleapis.com/v1/"
        f"projects/{project}/locations/{loc}/templates/{tpl}:{action}"
    )

    token = _get_adc_token()
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={body_key: {"text": (text or "")[:16000]}},
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"Model Armor HTTP {resp.status_code}: {resp.text[:300]}")

    payload = resp.json()
    sanitization = payload.get("sanitizationResult", {})
    match_state = str(sanitization.get("filterMatchState", "NO_MATCH_FOUND"))
    blocked = match_state == "MATCH_FOUND"
    verdict = ModelArmorVerdict(
        checked=True,
        blocked=blocked,
        match_state=match_state,
        template_id=tpl,
        detail=f"Model Armor {action}: {match_state}",
    )
    if blocked and raise_on_block:
        raise PromptBlockedError(f"Blocked by Google Cloud Model Armor ({tpl}): {match_state}")
    return verdict


class SlidingWindowRateLimiter:
    """Thread-safe per-IP sliding window rate limiter."""

    def __init__(self, max_requests_per_min: int = 60) -> None:
        self._limit = max(1, max_requests_per_min)
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, client_key: str) -> bool:
        now = time.monotonic()
        cutoff = now - 60.0
        with self._lock:
            q = self._hits[client_key]
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self._limit:
                return False
            q.append(now)
            return True


_JWKS_LOCK = threading.Lock()
_JWKS_CLIENT: PyJWKClient | None = None


def verify_iap_jwt(token: str, *, expected_audience: str) -> dict[str, Any]:
    """Verify Cloud IAP JWT strictly using hardcoded ES256 and Google's IAP JWKS."""
    global _JWKS_CLIENT
    if not token or not expected_audience:
        raise PermissionError("Missing Cloud IAP assertion header or expected audience")
    with _JWKS_LOCK:
        if _JWKS_CLIENT is None:
            _JWKS_CLIENT = PyJWKClient(IAP_JWKS_URL)
        signing_key = _JWKS_CLIENT.get_signing_key_from_jwt(token)

    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["ES256"],
        audience=expected_audience,
        issuer=IAP_ISSUER,
        options={"require": ["exp", "iat", "aud", "iss", "sub"]},
    )


class SecurityHeadersAndGuardMiddleware(BaseHTTPMiddleware):
    """Attaches strict CSP/security headers and enforces rate limits + optional Cloud IAP."""

    def __init__(self, app: Any, *, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings
        self._limiter = SlidingWindowRateLimiter(settings.rate_limit_per_min)

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        path = request.url.path
        if path != "/health":
            client_ip = request.client.host if request.client else "unknown"
            if path.startswith("/api/") and not self._limiter.allow(client_ip):
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded. Please retry shortly."},
                    headers={"Content-Security-Policy": STRICT_CSP_HEADER},
                )

            if self._settings.require_iap_auth and path.startswith("/api/"):
                iap_jwt = request.headers.get("x-goog-iap-jwt-assertion", "").strip()
                try:
                    claims = verify_iap_jwt(
                        iap_jwt,
                        expected_audience=self._settings.iap_expected_audience,
                    )
                    request.state.user_email = str(claims.get("email", claims.get("sub", "iap-user")))
                except Exception:
                    return JSONResponse(
                        status_code=401,
                        content={"detail": "Valid Cloud IAP authentication required."},
                        headers={"Content-Security-Policy": STRICT_CSP_HEADER},
                    )

        response = await call_next(request)
        response.headers["Content-Security-Policy"] = STRICT_CSP_HEADER
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response
