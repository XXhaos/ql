#!/usr/bin/env python3
"""NodeSeek HTTP bridge for QingLong.

Reads one JSON request from stdin and writes one JSON response to stdout.
Authentication cookies never appear in argv or logs.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

try:
    from curl_cffi import requests
except Exception as import_error:  # pragma: no cover - exercised on QingLong
    requests = None
    CURL_CFFI_IMPORT_ERROR = import_error
else:
    CURL_CFFI_IMPORT_ERROR = None


BASE_URL = "https://www.nodeseek.com"
BOARD_URL = f"{BASE_URL}/board"
DEFAULT_IMPERSONATE_CANDIDATES = (
    "chrome146",
    "chrome145",
    "chrome142",
    "chrome136",
    "chrome133a",
    "chrome131",
)
TRANSIENT_CLOUDFLARE_COOKIES = {"cf_clearance", "__cf_bm", "_cfuvid"}


def emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def parse_cookie_header(raw_cookie: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in raw_cookie.split(";"):
        name, separator, value = part.strip().partition("=")
        if not separator or not name:
            continue
        lowered = name.lower()
        if lowered in TRANSIENT_CLOUDFLARE_COOKIES or lowered.startswith("cf_chl_"):
            continue
        cookies[name] = value
    return cookies


def is_cloudflare_challenge(response: Any) -> bool:
    if response is None:
        return False
    mitigated = str(response.headers.get("cf-mitigated", "")).lower()
    server = str(response.headers.get("server", "")).lower()
    location = str(response.headers.get("location", ""))
    body = (response.text or "").lower()
    redirect_loop = (
        response.status_code in {301, 302, 303, 307, 308}
        and "cloudflare" in server
        and location == str(response.url)
    )
    return (
        mitigated == "challenge"
        or (response.status_code == 403 and "just a moment" in body)
        or "cf-chl-" in body
        or redirect_loop
    )


def candidates() -> list[str]:
    preferred = os.getenv("NS_IMPERSONATE", "").strip()
    values = ([preferred] if preferred else []) + list(DEFAULT_IMPERSONATE_CANDIDATES)
    return list(dict.fromkeys(value for value in values if value))


def request_once(version: str, payload: dict[str, Any]) -> Any:
    session = requests.Session(impersonate=version)
    try:
        for name, value in parse_cookie_header(payload["cookie"]).items():
            session.cookies.set(name, value, domain=".nodeseek.com", path="/")

        timeout = int(payload.get("timeout", 20))
        language = "zh-CN,zh;q=0.9,en;q=0.8"
        warmup = session.get(
            BOARD_URL,
            headers={"Accept-Language": language},
            timeout=timeout,
            allow_redirects=True,
        )
        if is_cloudflare_challenge(warmup):
            return warmup

        headers = {
            "Accept": "*/*",
            "Accept-Language": language,
            "Origin": BASE_URL,
            "Referer": BOARD_URL,
        }
        method = payload["method"]
        url = f"{BASE_URL}{payload['path']}"
        request_options: dict[str, Any] = {}
        if method == "POST":
            # Current NodeSeek accepts an empty JSON object. Supplying a real
            # body avoids Cloudflare's same-URL 303 response for an empty POST.
            headers["Content-Type"] = "application/json"
            request_options["json"] = {}
        return session.request(
            method,
            url,
            headers=headers,
            timeout=timeout,
            # A 301/302 must not silently turn the attendance POST into GET.
            # Return the redirect to the caller so it can be classified safely.
            allow_redirects=False,
            **request_options,
        )
    finally:
        session.close()


def main() -> None:
    if requests is None:
        emit(
            {
                "error": "missing_dependency",
                "message": "青龙缺少 Python 依赖 curl_cffi==0.15.0",
            }
        )
        return

    try:
        payload = json.load(sys.stdin)
        method = str(payload.get("method", "GET")).upper()
        path = str(payload.get("path", ""))
        cookie = payload.get("cookie")
        if method not in {"GET", "POST"}:
            raise ValueError("只允许 GET/POST")
        if not path.startswith("/") or "://" in path:
            raise ValueError("请求路径无效")
        if not isinstance(cookie, str) or not cookie:
            raise ValueError("Cookie 为空")
        payload["method"] = method

        last_response = None
        attempted: list[str] = []
        errors: list[str] = []
        for version in candidates():
            attempted.append(version)
            try:
                response = request_once(version, payload)
                last_response = response
            except Exception as error:
                errors.append(f"{version}: {type(error).__name__}")
                continue

            challenged = is_cloudflare_challenge(response)
            if challenged:
                continue
            emit(
                {
                    "status": response.status_code,
                    "ok": 200 <= response.status_code < 300,
                    "url": str(response.url),
                    "text": response.text,
                    "location": response.headers.get("location", ""),
                    "impersonate": version,
                    "cloudflareChallenge": False,
                    "attempts": len(attempted),
                }
            )
            return

        if last_response is not None:
            emit(
                {
                    "status": last_response.status_code,
                    "ok": False,
                    "url": str(last_response.url),
                    "text": last_response.text,
                    "location": last_response.headers.get("location", ""),
                    "impersonate": attempted[-1],
                    "cloudflareChallenge": is_cloudflare_challenge(last_response),
                    "attempts": len(attempted),
                }
            )
            return

        emit(
            {
                "error": "request_failed",
                "message": "curl_cffi 请求失败",
                "attempts": len(attempted),
                "details": errors[-3:],
            }
        )
    except Exception as error:
        emit(
            {
                "error": "invalid_request",
                "message": f"{type(error).__name__}: {error}",
            }
        )


if __name__ == "__main__":
    main()
