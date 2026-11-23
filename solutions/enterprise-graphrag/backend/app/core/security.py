"""Web security middleware, sliding-window rate limiter, IAP ES256 JWT verifier, and Model Armor client.

Enforces mandatory-secure-web-skills controls:
  - Strict CSP (no unsafe-inline / unsafe-eval), X-Frame-Options: DENY, nosniff, Referrer-Policy
  - HTTP method allowlist (GET, POST, OPTIONS, HEAD only)
  - Hardcoded ES256 Cloud IAP JWT verification (rejects alg:none, verifies iss/aud/exp)
  - Sliding-window per-client rate limiting
  - Real Google Cloud Model Armor REST v1 prompt/response screening (zero regex fallbacks)
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import httpx
import jwt
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import Settings

IAP_JWT_HEADER = "x-goog-iap-jwt-assertion"
IAP_ISSUER = "https://cloud.google.com/iap"
IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
IAP_CERTS_TTL_SECONDS = 3600.0
ALLOWED_HTTP_METHODS: frozenset[str] = frozenset({"GET", "POST", "OPTIONS", "HEAD"})

STRICT_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

_LOCATION_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
_SHORT_TEMPLATE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
_PROJECT_NUMBER_RE = re.compile(r"projects/\d{6,}")
_RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})
_MAX_SANITIZE_CHARS = 16_000


class AuthError(ValueError):
    """Caller failed IAP JWT authentication (maps to HTTP 401)."""


class PromptBlockedError(ValueError):
    """Google Cloud Model Armor blocked a prompt or document excerpt (maps to HTTP 400)."""

    def __init__(self, message: str, *, matched_filters: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.matched_filters = matched_filters


class ModelArmorError(RuntimeError):
    """Model Armor REST invocation or response parsing error."""


@dataclass(frozen=True)
class ModelArmorVerdict:
    """Honest Model Armor inspection outcome surfaced in stage telemetry and UI badges."""

    checked: bool
    blocked: bool
    match_state: str
    matched_filters: tuple[str, ...] = ()
    reason: str = ""
    latency_ms: float = 0.0


# ---------------------------------------------------------------------------
# Cloud IAP ES256 JWT Verification
# ---------------------------------------------------------------------------

_IAP_CERT_LOCK = threading.Lock()
_IAP_CERT_CACHE: dict[str, Any] = {"fetched_at": 0.0, "certs": {}}


def _fetch_iap_public_certs(timeout_s: float = 5.0) -> dict[str, str]:
    now = time.monotonic()
    with _IAP_CERT_LOCK:
        cached = _IAP_CERT_CACHE["certs"]
        if cached and (now - float(_IAP_CERT_CACHE["fetched_at"])) < IAP_CERTS_TTL_SECONDS:
            return dict(cached)
    resp = httpx.get(IAP_CERTS_URL, timeout=timeout_s)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or not data:
        raise AuthError("IAP public key response is invalid")
    certs = {str(k): str(v) for k, v in data.items()}
    with _IAP_CERT_LOCK:
        _IAP_CERT_CACHE["fetched_at"] = now
        _IAP_CERT_CACHE["certs"] = certs
    return dict(certs)


def _validate_jwt_header_es256(token: str) -> dict[str, Any]:
    """Decode JWT header strictly and reject any algorithm other than ES256 (including alg=none)."""
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise AuthError("Malformed IAP JWT assertion")
    padded = parts[0] + "=" * (-len(parts[0]) % 4)
    try:
        header = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception as exc:
        raise AuthError("Malformed IAP JWT header") from exc
    if not isinstance(header, dict) or header.get("alg") != "ES256":
        raise AuthError("IAP JWT must use alg=ES256")
    return header


def verify_iap_jwt(
    token: str,
    audience: str,
    *,
    certs_override: Mapping[str, str] | None = None,
) -> str:
    """Cryptographically verify a Cloud IAP ES256 JWT and return authenticated subject/email."""
    if not isinstance(token, str) or not token.strip():
        raise AuthError("Missing X-Goog-IAP-JWT-Assertion header")
    if not isinstance(audience, str) or not audience.strip():
        raise AuthError("IAP_JWT_AUDIENCE is not configured")

    raw = token.strip()
    header = _validate_jwt_header_es256(raw)
    kid = str(header.get("kid") or "").strip()

    certs = dict(certs_override) if certs_override is not None else _fetch_iap_public_certs()
    if kid:
        key_pem = certs.get(kid)
        if not key_pem:
            raise AuthError("Unknown IAP signing key id")
        candidate_keys = [key_pem]
    else:
        candidate_keys = list(certs.values())

    last_err: Exception | None = None
    claims: dict[str, Any] | None = None
    for key_pem in candidate_keys:
        try:
            # Hardcoded algorithms=["ES256"] — never derive algorithm from unverified token!
            claims = jwt.decode(
                raw,
                key_pem,
                algorithms=["ES256"],
                audience=audience.strip(),
                issuer=IAP_ISSUER,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
            break
        except jwt.PyJWTError as exc:
            last_err = exc

    if claims is None:
        raise AuthError("Invalid or expired IAP JWT assertion") from last_err

    identity = str(claims.get("email") or claims.get("sub") or "").strip()
    if not identity:
        raise AuthError("IAP JWT assertion missing subject identity")
    return identity


# ---------------------------------------------------------------------------
# Sliding-Window Rate Limiter
# ---------------------------------------------------------------------------


class SlidingWindowRateLimiter:
    """In-memory per-client sliding-window rate limiter.

    TODO(security): For multi-instance Cloud Run autoscaling in production,
    complement this per-instance limiter with Cloud Armor or Memorystore Redis.
    """

    def __init__(self, *, max_requests: int = 30, window_seconds: float = 60.0) -> None:
        if max_requests < 1:
            raise ValueError("max_requests must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self._max_requests = max_requests
        self._window = window_seconds
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, client_key: str, *, now: float | None = None) -> tuple[bool, int]:
        """Return (allowed, retry_after_seconds)."""
        ts = time.monotonic() if now is None else now
        cutoff = ts - self._window
        with self._lock:
            bucket = self._hits[client_key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self._max_requests:
                retry_after = max(1, int(self._window - (ts - bucket[0])) + 1)
                return False, retry_after
            bucket.append(ts)
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


# ---------------------------------------------------------------------------
# Google Cloud Model Armor REST v1 Client
# ---------------------------------------------------------------------------


def _redact_diagnostic(text: str, *, project_id: str, template_id: str) -> str:
    out = str(text or "")
    for val, placeholder in (
        (template_id.strip(), "${MODEL_ARMOR_TEMPLATE_ID}"),
        (project_id.strip(), "${GCP_PROJECT_ID}"),
    ):
        if val:
            out = out.replace(val, placeholder)
    return _PROJECT_NUMBER_RE.sub("projects/${GCP_PROJECT_NUMBER}", out)


def _get_adc_bearer_token() -> str:
    import google.auth
    from google.auth.transport.requests import Request as GoogleAuthRequest

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    if not creds.valid:
        creds.refresh(GoogleAuthRequest())
    if not creds.token:
        raise ModelArmorError("Unable to obtain Google Cloud ADC bearer token for Model Armor")
    return str(creds.token)


def parse_model_armor_response(payload: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    """Strictly parse Model Armor sanitizationResult JSON."""
    result = payload.get("sanitizationResult")
    if not isinstance(result, Mapping):
        raise ModelArmorError("Model Armor response missing sanitizationResult")
    match_state = str(result.get("filterMatchState") or "NO_MATCH_FOUND")
    if match_state not in ("NO_MATCH_FOUND", "MATCH_FOUND"):
        raise ModelArmorError(f"Unexpected Model Armor filterMatchState: {match_state!r}")

    matched: list[str] = []
    filter_results = result.get("filterResults")
    if isinstance(filter_results, Mapping):
        for fname, fbody in filter_results.items():
            if not isinstance(fbody, Mapping):
                continue
            # Filter entries wrap one inner result dict (e.g. piAndJailbreakFilterResult)
            inner_state = str(fbody.get("matchState") or "")
            if inner_state == "MATCH_FOUND":
                matched.append(str(fname))
                continue
            for sub in fbody.values():
                if isinstance(sub, Mapping) and str(sub.get("matchState") or "") == "MATCH_FOUND":
                    matched.append(str(fname))
                    break
    return match_state, tuple(sorted(set(matched)))


def screen_with_model_armor(
    *,
    settings: Settings,
    text: str,
    mode: str = "user_prompt",
    token_provider: Callable[[], str] | None = None,
    http_post: Callable[..., httpx.Response] | None = None,
) -> ModelArmorVerdict:
    """Screen untrusted user prompt or document text via Google Cloud Model Armor REST v1.

    Zero regex/keyword fallbacks: when MODEL_ARMOR_TEMPLATE_ID is unset, explicitly returns
    checked=False so callers report honest telemetry.
    """
    template_id = settings.model_armor_template_id.strip()
    if not template_id:
        return ModelArmorVerdict(
            checked=False,
            blocked=False,
            match_state="NOT_CONFIGURED",
            reason="MODEL_ARMOR_TEMPLATE_ID not configured",
        )
    if not settings.gcp_project_id:
        return ModelArmorVerdict(
            checked=False,
            blocked=False,
            match_state="NOT_CONFIGURED",
            reason="GCP_PROJECT_ID not configured",
        )

    loc = settings.model_armor_location.strip()
    if not _LOCATION_RE.fullmatch(loc):
        raise ValueError(f"Invalid MODEL_ARMOR_LOCATION: {loc!r}")
    if not _SHORT_TEMPLATE_ID_RE.fullmatch(template_id):
        raise ValueError(f"Invalid MODEL_ARMOR_TEMPLATE_ID: {template_id!r}")

    if mode == "model_response":
        action = "sanitizeModelResponse"
        body: dict[str, Any] = {"modelResponseData": {"text": text[:_MAX_SANITIZE_CHARS]}}
    else:
        action = "sanitizeUserPrompt"
        body = {"userPromptData": {"text": text[:_MAX_SANITIZE_CHARS]}}

    url = (
        f"https://modelarmor.{loc}.rep.googleapis.com/v1/"
        f"projects/{settings.gcp_project_id}/locations/{loc}/templates/{template_id}:{action}"
    )
    bearer = (token_provider or _get_adc_bearer_token)()
    post_fn = http_post or httpx.post

    t0 = time.perf_counter()
    last_exc: Exception | None = None
    for attempt in range(1, 4):
        try:
            resp = post_fn(
                url,
                headers={
                    "Authorization": f"Bearer {bearer}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=10.0,
            )
            if resp.status_code in _RETRYABLE_STATUS and attempt < 3:
                time.sleep(0.3 * (2 ** (attempt - 1)))
                continue
            if resp.status_code >= 400:
                safe_msg = _redact_diagnostic(
                    resp.text[:300], project_id=settings.gcp_project_id, template_id=template_id
                )
                raise ModelArmorError(f"Model Armor HTTP {resp.status_code}: {safe_msg}")
            match_state, matched_filters = parse_model_armor_response(resp.json())
            latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            blocked = match_state == "MATCH_FOUND"
            verdict = ModelArmorVerdict(
                checked=True,
                blocked=blocked,
                match_state=match_state,
                matched_filters=matched_filters,
                reason="Blocked by Google Cloud Model Armor" if blocked else "Clean",
                latency_ms=latency_ms,
            )
            if blocked:
                raise PromptBlockedError(
                    "Input rejected by Google Cloud Model Armor policy.",
                    matched_filters=matched_filters,
                )
            return verdict
        except (PromptBlockedError, ModelArmorError):
            raise
        except Exception as exc:
            last_exc = exc
            if attempt >= 3:
                safe_msg = _redact_diagnostic(
                    str(exc), project_id=settings.gcp_project_id, template_id=template_id
                )
                raise ModelArmorError(f"Model Armor request failed: {safe_msg}") from exc
            time.sleep(0.3 * (2 ** (attempt - 1)))

    raise ModelArmorError("Model Armor request exhausted retries") from last_exc


# ---------------------------------------------------------------------------
# FastAPI Security Headers, Method Allowlist, Rate Limit & IAP Middleware
# ---------------------------------------------------------------------------


def _client_key(request: Request) -> str:
    identity = getattr(request.state, "caller_identity", "")
    if identity:
        return f"id:{identity}"
    fwd = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    if fwd:
        return f"ip:{fwd}"
    if request.client and request.client.host:
        return f"ip:{request.client.host}"
    return "ip:unknown"


class SecurityMiddleware(BaseHTTPMiddleware):
    """Enforces method allowlist, strict CSP/security headers, IAP JWT auth, and rate limits."""

    def __init__(
        self,
        app: Any,
        *,
        settings: Settings,
        write_limiter: SlidingWindowRateLimiter | None = None,
        read_limiter: SlidingWindowRateLimiter | None = None,
    ) -> None:
        super().__init__(app)
        self._settings = settings
        self._write_limiter = write_limiter or SlidingWindowRateLimiter(max_requests=30, window_seconds=60.0)
        self._read_limiter = read_limiter or SlidingWindowRateLimiter(max_requests=180, window_seconds=60.0)

    def _apply_headers(self, response: Response, *, is_api: bool) -> Response:
        response.headers["Content-Security-Policy"] = STRICT_CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if is_api:
            response.headers["Cache-Control"] = "no-store"
        return response

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        path = request.url.path
        is_api = path.startswith("/api/")

        if request.method.upper() not in ALLOWED_HTTP_METHODS:
            resp = JSONResponse({"detail": "Method not allowed"}, status_code=405)
            return self._apply_headers(resp, is_api=is_api)

        # Optional Cloud IAP JWT enforcement (exempt /health for Cloud Run probes)
        if self._settings.require_iap_jwt and path != "/health":
            certs_override = getattr(request.app.state, "iap_certs_override", None)
            try:
                identity = verify_iap_jwt(
                    request.headers.get(IAP_JWT_HEADER, ""),
                    self._settings.iap_jwt_audience,
                    certs_override=certs_override,
                )
                request.state.caller_identity = identity
            except AuthError as exc:
                resp = JSONResponse({"detail": str(exc)}, status_code=401)
                return self._apply_headers(resp, is_api=is_api)

        # Sliding-window rate limiting on all API routes
        if is_api:
            limiter = self._write_limiter if request.method.upper() == "POST" else self._read_limiter
            allowed, retry_after = limiter.check(_client_key(request))
            if not allowed:
                resp = JSONResponse(
                    {"detail": "Rate limit exceeded. Please retry shortly."},
                    status_code=429,
                    headers={"Retry-After": str(retry_after)},
                )
                return self._apply_headers(resp, is_api=True)

        response = await call_next(request)
        return self._apply_headers(response, is_api=is_api)
