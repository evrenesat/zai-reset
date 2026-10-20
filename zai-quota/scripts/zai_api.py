"""HTTP layer for the Z.ai quota / reset-card business API.

- ``urllib.request`` only (stdlib), explicit 10 second timeout.
- The transport is injectable: any callable ``request -> (status:int, body:bytes)``
  so tests run fully offline. The default transport uses ``urllib``.
- Envelope checks verify the business response (``success``/``code``), not just
  the HTTP status.
- Error taxonomy matters for the reset algorithm: ``definite`` errors are safe
  API/business rejections (the POST outcome is known); non-definite errors
  (timeout, lost connection, 5xx, unreadable body) mean the POST outcome is
  UNKNOWN and must surface as ``uncertain``, never as a retry.
- No header values, bodies or secrets are ever logged here.
"""

from __future__ import annotations

import json as _json
import socket
import urllib.error
import urllib.request

QUOTA_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
CARDS_URL = "https://api.z.ai/api/biz/customer-package-reset/list?targetType=PERSONAL"
RESET_URL = "https://api.z.ai/api/biz/customer-package-reset/use"

DEFAULT_TIMEOUT_S = 10.0
_MAX_MESSAGE_CHARS = 200


class ApiError(Exception):
    """Base API error. ``definite`` True => outcome known (safe rejection)."""

    definite = True

    def __init__(self, reason: str, message: str = "", http_status=None):
        super().__init__(message or reason)
        self.reason = reason
        self.message = (message or reason)[:_MAX_MESSAGE_CHARS]
        self.http_status = http_status


class TransportError(ApiError):
    """Request did not complete (timeout / lost connection). Outcome unknown."""

    definite = False


class HttpError(ApiError):
    """Non-2xx HTTP status. 4xx is a definite rejection; 5xx is uncertain."""

    def __init__(self, reason: str, message: str = "", http_status=None):
        super().__init__(reason, message, http_status)
        self.definite = http_status is not None and http_status < 500


class BusinessError(ApiError):
    """HTTP OK but the business envelope rejected the request (definite)."""

    pass


class ParseError(ApiError):
    """Body could not be decoded/parsed; for a POST this means unreadable."""

    definite = False


def _short(value) -> str:
    text = str(value)
    return text[:_MAX_MESSAGE_CHARS]


def default_transport(request, timeout_s: float = DEFAULT_TIMEOUT_S):
    """urllib-based transport returning (status, body bytes)."""
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:  # must precede URLError (subclass)
        try:
            body = exc.read()
        except Exception:  # pragma: no cover - read failure on error body
            body = b""
        return int(exc.code), body
    except (socket.timeout, TimeoutError) as exc:
        raise TransportError("transport_timeout", _short(exc)) from exc
    except (urllib.error.URLError, ConnectionError, OSError) as exc:
        raise TransportError("transport_error", _short(exc)) from exc


def _build_request(url: str, api_key: str, method: str = "GET", body=None):
    headers = {
        "Authorization": "Bearer %s" % api_key,
        "Accept": "application/json",
    }
    data = None
    if body is not None:
        data = body
        headers["Content-Type"] = "application/json"
    return urllib.request.Request(url, data=data, headers=headers, method=method)


def _provider_message(payload) -> str:
    """Best-effort safe provider message; never echoes auth material."""
    if not isinstance(payload, dict):
        return ""
    for key in ("msg", "message", "error"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:_MAX_MESSAGE_CHARS]
    return ""


def _call(url, api_key, method="GET", body=None, transport=None):
    """Send one request and return the decoded JSON payload, or raise ApiError."""
    transport = transport or default_transport
    request = _build_request(url, api_key, method, body)
    try:
        status, raw = transport(request)
    except ApiError:
        raise
    except Exception as exc:  # stub transports may raise raw errors (e.g. TimeoutError)
        raise TransportError("transport_error", _short(exc)) from exc
    if not isinstance(status, int):
        raise ParseError("invalid_transport_status", "transport returned a non-int status")
    if status >= 400:
        payload = None
        try:
            payload = _json.loads(raw.decode("utf-8"))
        except Exception:
            payload = None
        reason = "http_server_error" if status >= 500 else "http_client_error"
        raise HttpError(
            reason,
            _provider_message(payload) or ("HTTP %d" % status),
            http_status=status,
        )
    try:
        payload = _json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ParseError("malformed_json", _short(exc)) from exc
    return payload


def _require_envelope(payload, allowed_codes):
    """Verify the business envelope: success is true and code is accepted."""
    if not isinstance(payload, dict):
        raise ParseError("response_not_json_object", "expected a JSON object")
    if payload.get("success") is not True or payload.get("code") not in allowed_codes:
        raise BusinessError(
            "business_rejected",
            _provider_message(payload) or "business envelope rejected the request",
        )
    return payload


def fetch_quota_payload(api_key, *, transport=None):
    """GET the read-only quota/usage endpoint (five-hour + other windows)."""
    return _require_envelope(_call(QUOTA_URL, api_key, "GET", transport=transport), (200,))


def fetch_cards_payload(api_key, *, transport=None):
    """GET the personal reset-card inventory (read-only)."""
    return _require_envelope(_call(CARDS_URL, api_key, "GET", transport=transport), (200,))


def post_reset_card(api_key, record_id, request_id, *, transport=None):
    """POST to redeem one five-hour reset card. Only reset_please may call this.

    Returns the acknowledged payload on definitive success (``success`` true and
    ``code`` 0 or 200). Definite rejections raise BusinessError/definite
    HttpError; unknown outcomes raise non-definite errors (TransportError,
    5xx HttpError, ParseError).
    """
    body = _json.dumps(
        {
            "targetType": "PERSONAL",
            "resetType": "FIVE_HOUR",
            "recordId": record_id,
            "requestId": request_id,
        }
    ).encode("utf-8")
    payload = _call(RESET_URL, api_key, "POST", body=body, transport=transport)
    return _require_envelope(payload, (0, 200))
