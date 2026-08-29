#!/usr/bin/env python3
"""NodeSeek HTTP bridge for QingLong.

Reads one JSON request from stdin and writes one JSON response to stdout.
Authentication cookies never appear in argv or task logs. Fast requests use
curl_cffi; a Cloudflare challenge falls back to the internal Camoufox service,
which performs the target API request in the same real-browser context.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

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
BROWSER_API_URL = os.getenv(
    "NODESEEK_BROWSER_URL", "http://nodeseek-browser:8191/nodeseek"
).strip()
MAX_BROWSER_RESPONSE_BYTES = 2 * 1024 * 1024


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
        user_agent = str(payload.get("userAgent") or "").strip()
        language = "zh-CN,zh;q=0.9,en;q=0.8"
        warmup_headers = {"Accept-Language": language}
        if user_agent:
            warmup_headers["User-Agent"] = user_agent
        warmup = session.get(
            BOARD_URL,
            headers=warmup_headers,
            timeout=timeout,
            allow_redirects=True,
        )
        if is_cloudflare_challenge(warmup):
            return warmup

        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": language,
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/",
            "X-Requested-With": "XMLHttpRequest",
        }
        if user_agent:
            headers["User-Agent"] = user_agent
        method = payload["method"]
        url = f"{BASE_URL}{payload['path']}"
        request_options: dict[str, Any] = {}
        if method == "POST":
            # NodeSeek's own attendance request is a POST with an empty body.
            # Adding an artificial JSON object changes the request semantics.
            request_options["data"] = b""
        return session.request(
            method,
            url,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
            **request_options,
        )
    finally:
        session.close()


def request_in_browser(payload: dict[str, Any]) -> dict[str, Any]:
    if not BROWSER_API_URL.startswith(("http://", "https://")):
        raise ValueError("NODESEEK_BROWSER_URL 必须是 HTTP(S) 地址")
    timeout_seconds = int(payload.get("timeout", 20))
    body = json.dumps(
        {
            "path": payload["path"],
            "method": payload["method"],
            "cookie": payload["cookie"],
            "userAgent": payload.get("userAgent", ""),
            "userId": payload.get("userId", ""),
            "timeout": max(30, min(timeout_seconds * 6, 120)),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    api_request = urlrequest.Request(
        BROWSER_API_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(
            api_request,
            timeout=max(40, min(timeout_seconds * 7, 140)),
        ) as response:
            raw = response.read(MAX_BROWSER_RESPONSE_BYTES + 1)
    except urlerror.HTTPError as error:
        try:
            detail = json.loads(error.read(64 * 1024)).get("detail", "")
        except Exception:
            detail = ""
        suffix = f"：{str(detail)[:160]}" if detail else ""
        raise RuntimeError(f"真实浏览器后备返回 HTTP {error.code}{suffix}") from error
    except (OSError, urlerror.URLError) as error:
        raise RuntimeError(f"真实浏览器后备服务不可用：{type(error).__name__}") from error
    if len(raw) > MAX_BROWSER_RESPONSE_BYTES:
        raise RuntimeError("真实浏览器后备响应过大")
    try:
        result = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("真实浏览器后备返回格式异常") from error
    if not isinstance(result, dict):
        raise RuntimeError("真实浏览器后备返回的不是对象")
    required = {"status", "ok", "url", "text", "cloudflareChallenge"}
    if not required.issubset(result):
        raise RuntimeError("真实浏览器后备缺少必要字段")
    return result


def response_payload(response: Any, version: str, attempts: int) -> dict[str, Any]:
    return {
        "status": response.status_code,
        "ok": 200 <= response.status_code < 300,
        "url": str(response.url),
        "text": response.text,
        "location": response.headers.get("location", ""),
        "impersonate": version,
        "cloudflareChallenge": False,
        "attempts": attempts,
    }


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
        user_agent = payload.get("userAgent", "")
        user_id = payload.get("userId", "")
        if method not in {"GET", "POST"}:
            raise ValueError("只允许 GET/POST")
        if not path.startswith("/") or "://" in path:
            raise ValueError("请求路径无效")
        if not isinstance(cookie, str) or not cookie:
            raise ValueError("Cookie 为空")
        if not isinstance(user_agent, str) or "\n" in user_agent or "\r" in user_agent:
            raise ValueError("User-Agent 格式无效")
        if not isinstance(user_id, str) or (user_id and not user_id.isdigit()):
            raise ValueError("用户 ID 格式无效")
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

            if is_cloudflare_challenge(response):
                # A managed challenge requires browser execution. Cycling more
                # TLS profiles only creates a suspicious burst from the same IP.
                break
            emit(response_payload(response, version, len(attempted)))
            return

        browser_fallback_error = ""
        try:
            browser_result = request_in_browser(payload)
            browser_result.update(
                {
                    "impersonate": "camoufox",
                    "browserFallback": "direct",
                    "attempts": len(attempted) + 1,
                }
            )
            emit(browser_result)
            return
        except Exception as error:
            browser_fallback_error = str(error)[:180]

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
                    "browserFallbackError": browser_fallback_error,
                }
            )
            return

        emit(
            {
                "error": "request_failed",
                "message": browser_fallback_error or "curl_cffi 请求失败",
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
