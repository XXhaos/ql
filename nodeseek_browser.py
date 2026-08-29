#!/usr/bin/env python3
"""Restricted Camoufox endpoint for NodeSeek QingLong requests.

The service is intentionally limited to three NodeSeek API paths and must only
be attached to QingLong's internal Docker network. It never logs request bodies.
"""

from __future__ import annotations

import asyncio
import re
from http import HTTPStatus
from typing import Any

from fastapi import Depends, HTTPException, Request
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright_captcha import CaptchaType

from main import app
from src.consts import CHALLENGE_TITLES
from src.utils import CamoufoxDepClass, get_camoufox


BASE_URL = "https://www.nodeseek.com"
BOARD_URL = f"{BASE_URL}/board"
ALLOWED_PATHS = (
    re.compile(r"^/api/attendance\?random=(?:true|false)$"),
    re.compile(r"^/api/attendance/board\?page=1$"),
    re.compile(r"^/api/account/getInfo/\d+\?readme=1$"),
)
COOKIE_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
TRANSIENT_CLOUDFLARE_COOKIES = {"__cf_bm", "_cfuvid"}
MAX_COOKIE_BYTES = 48 * 1024
MAX_RESPONSE_CHARS = 2 * 1024 * 1024


def parse_cookie_header(raw_cookie: str) -> list[dict[str, Any]]:
    if len(raw_cookie.encode("utf-8")) > MAX_COOKIE_BYTES:
        raise HTTPException(status_code=400, detail="Cookie 过大")
    cookies: list[dict[str, Any]] = []
    for part in raw_cookie.split(";"):
        name, separator, value = part.strip().partition("=")
        if not separator or not COOKIE_NAME.fullmatch(name):
            continue
        lowered = name.lower()
        if lowered in TRANSIENT_CLOUDFLARE_COOKIES or lowered.startswith("cf_chl_"):
            continue
        if "\r" in value or "\n" in value:
            raise HTTPException(status_code=400, detail="Cookie 格式无效")
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": ".nodeseek.com",
                "path": "/",
                "secure": True,
            }
        )
    if not cookies:
        raise HTTPException(status_code=400, detail="Cookie 为空")
    return cookies


async def wait_for_page(page: Any, timeout_seconds: int) -> None:
    timeout_ms = timeout_seconds * 1000
    await page.goto(BOARD_URL, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        pass


async def solve_challenge(dep: CamoufoxDepClass, timeout_seconds: int) -> None:
    if await dep.page.title() not in CHALLENGE_TITLES:
        return
    try:
        await asyncio.wait_for(
            dep.solver.solve_captcha(
                captcha_container=dep.page,
                captcha_type=CaptchaType.CLOUDFLARE_INTERSTITIAL,
                wait_checkbox_attempts=1,
                wait_checkbox_delay=0.5,
            ),
            timeout=timeout_seconds,
        )
        await dep.page.wait_for_function(
            "titles => !titles.includes(document.title)",
            arg=list(CHALLENGE_TITLES),
            timeout=timeout_seconds * 1000,
        )
    except (TimeoutError, asyncio.TimeoutError, PlaywrightTimeoutError) as error:
        raise HTTPException(
            status_code=504, detail="Cloudflare 浏览器挑战超时"
        ) from error
    if await dep.page.title() in CHALLENGE_TITLES:
        raise HTTPException(status_code=502, detail="Cloudflare 浏览器挑战未通过")


async def wait_for_fog_cookie(dep: CamoufoxDepClass, timeout_seconds: int) -> None:
    """Wait for NodeSeek's delayed proof-of-work cookie.

    The current page starts its fog proof after ten seconds. Calling a
    session-scoped API before that proof finishes misleadingly returns
    ``USER NOT FOUND``, even when the account cookies are valid.
    """

    deadline = asyncio.get_running_loop().time() + min(timeout_seconds, 45)
    while asyncio.get_running_loop().time() < deadline:
        cookies = await dep.context.cookies(BASE_URL)
        if any(cookie.get("name") == "fog" and cookie.get("value") for cookie in cookies):
            return
        await asyncio.sleep(0.5)
    raise HTTPException(status_code=504, detail="NodeSeek fog 验证超时")


async def provide_fog_activity(dep: CamoufoxDepClass) -> None:
    """Provide the movement sample requested by NodeSeek's fog proof."""

    points = (
        (92, 86),
        (168, 121),
        (271, 164),
        (388, 211),
        (512, 274),
        (641, 326),
        (734, 387),
        (802, 443),
        (871, 398),
        (765, 332),
        (623, 286),
        (481, 241),
    )
    for x, y in points:
        await dep.page.mouse.move(x, y, steps=3)
        await asyncio.sleep(0.12)


@app.post("/nodeseek")
async def nodeseek_request(
    request: Request,
    dep: CamoufoxDepClass = Depends(get_camoufox),
) -> dict[str, Any]:
    content_length = int(request.headers.get("content-length") or 0)
    if content_length <= 0 or content_length > 64 * 1024:
        raise HTTPException(status_code=400, detail="请求大小无效")
    try:
        body = await request.json()
    except Exception as error:
        raise HTTPException(status_code=400, detail="请求格式无效") from error
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="请求格式无效")

    method = str(body.get("method") or "GET").upper()
    path = str(body.get("path") or "")
    raw_cookie = body.get("cookie")
    user_id = str(body.get("userId") or "")
    try:
        timeout_seconds = max(30, min(int(body.get("timeout") or 120), 120))
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail="超时时间无效") from error
    if method not in {"GET", "POST"}:
        raise HTTPException(status_code=400, detail="请求方法无效")
    if not any(pattern.fullmatch(path) for pattern in ALLOWED_PATHS):
        raise HTTPException(status_code=400, detail="请求路径不在允许列表")
    if not isinstance(raw_cookie, str) or not raw_cookie:
        raise HTTPException(status_code=400, detail="Cookie 为空")
    if user_id and not user_id.isdigit():
        raise HTTPException(status_code=400, detail="用户 ID 格式无效")

    await dep.context.add_cookies(parse_cookie_header(raw_cookie))
    await wait_for_page(dep.page, timeout_seconds)
    await solve_challenge(dep, timeout_seconds)
    try:
        await dep.page.wait_for_load_state("domcontentloaded", timeout=timeout_seconds * 1000)
    except PlaywrightTimeoutError:
        pass
    await provide_fog_activity(dep)
    await wait_for_fog_cookie(dep, timeout_seconds)

    try:
        result = await dep.page.evaluate(
            """
            async ({path, method, userId}) => {
              const requestOne = async (requestPath, requestMethod = "GET") => {
                const options = {
                  method: requestMethod,
                  credentials: "include",
                  redirect: "follow",
                  referrer: `${location.origin}/`,
                  referrerPolicy: "strict-origin-when-cross-origin",
                  headers: {
                    "Accept": "application/json, text/plain, */*",
                    "X-Requested-With": "XMLHttpRequest",
                  },
                };
                // Match NodeSeek's page request exactly: attendance is an
                // empty POST. An artificial JSON body changes its semantics.
                const response = await fetch(requestPath, options);
                return {
                  status: response.status,
                  ok: response.ok,
                  url: response.url,
                  text: await response.text(),
                  location: response.headers.get("location") || "",
                  cloudflareChallenge:
                    response.headers.get("cf-mitigated") === "challenge",
                };
              };

              const primary = await requestOne(path, method);
              // A successful attendance call is followed by the profile and
              // ranking queries in the same verified browser context. This
              // avoids solving Cloudflare and fog three times per account.
              if (method === "POST" && path.startsWith("/api/attendance?") && primary.ok) {
                primary.prefetched = {
                  profile: userId
                    ? await requestOne(`/api/account/getInfo/${userId}?readme=1`)
                    : null,
                  board: await requestOne("/api/attendance/board?page=1"),
                };
              }
              return primary;
            }
            """,
            {"path": path, "method": method, "userId": user_id},
        )
    except Exception as error:
        raise HTTPException(status_code=502, detail="浏览器内 API 请求失败") from error
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail="浏览器内 API 返回异常")
    text = str(result.get("text") or "")
    if len(text) > MAX_RESPONSE_CHARS:
        raise HTTPException(status_code=502, detail="NodeSeek 响应过大")
    prefetched = result.get("prefetched")
    if isinstance(prefetched, dict):
        for response in prefetched.values():
            if response is not None and len(str(response.get("text") or "")) > MAX_RESPONSE_CHARS:
                raise HTTPException(status_code=502, detail="NodeSeek 响应过大")
    if result.get("status") == HTTPStatus.FORBIDDEN and "Just a moment" in text:
        result["cloudflareChallenge"] = True
    return result
