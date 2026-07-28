/**
 * NodeSeek 青龙签到
 *
 * 必填环境变量：NODESEEK_DATA（兼容 nodeseek_data、NODESEEK_COOKIE）
 *   - 推荐沿用 Surge 数据格式：
 *     [{"userId":123,"userName":"账号一","token":"session=...","userAgent":"..."}]
 *   - NODESEEK_COOKIE 也可直接填写完整 Cookie；多账号可使用 JSON 数组或每行一个 Cookie。
 *   - Cookie 本身使用分号分隔，脚本不会用分号或 & 拆分账号。
 *
 * 可选环境变量：
 *   NODESEEK_USER_AGENT  浏览器 User-Agent；多账号时可填写 JSON 数组，或在上面的账号对象中单独设置。
 *   NODESEEK_RANDOM      false（默认，固定鸡腿 x 5）/ true（试试手气）。
 *
 * 建议定时：10 0 * * *（与原 Surge 模块一致）
 */

"use strict";

const { execFile } = require("child_process");
const path = require("path");

const BASE_URL = "https://www.nodeseek.com";
const BOARD_URL = `${BASE_URL}/board`;
const DEFAULT_USER_AGENT =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";
const REQUEST_TIMEOUT_MS = 20000;

class AccountExpiredError extends Error {
  constructor(message) {
    super(message);
    this.name = "AccountExpiredError";
  }
}

class CloudflareChallengeError extends Error {
  constructor(message) {
    super(message);
    this.name = "CloudflareChallengeError";
  }
}

async function pushFromQingLong(title, content) {
  try {
    if (global.QLAPI?.systemNotify) {
      const result = await global.QLAPI.systemNotify({ title, content });
      if (result?.code !== undefined && result.code !== 200) {
        throw new Error(result.message || `系统通知接口返回 ${result.code}`);
      }
      console.log("青龙系统推送已提交");
      return;
    }

    // 兼容未提供 QLAPI.systemNotify 的旧版青龙。
    const { sendNotify } = require("./sendNotify");
    await sendNotify(title, content);
    console.log("青龙兼容推送已提交");
  } catch (error) {
    console.error(`青龙推送失败：${error.message}`);
  }
}

function cleanText(value, maxLength = 240) {
  if (value === undefined || value === null) return "";
  const text =
    typeof value === "string"
      ? value
      : (() => {
          try {
            return JSON.stringify(value);
          } catch {
            return String(value);
          }
        })();
  return text.replace(/[\r\n\t]+/g, " ").trim().slice(0, maxLength);
}

function looksLikeExpired(message) {
  return /(?:未登录|请(?:先)?登录|登录后|重新登录|登录.*(?:失效|过期)|账号.*过期|会话.*(?:失效|过期)|cookie.*(?:失效|过期|无效)|unauthori[sz]ed|not\s+(?:logged\s*in|login)|user\s+not\s+found|sign\s*in|session.*(?:expired|invalid)|authentication\s+required)/i.test(
    message
  );
}

function looksLikeAlreadySigned(message) {
  return /(?:已经|已|重复).{0,6}签(?:到)?|签(?:到)?.{0,6}(?:已经|已|重复)|already.{0,12}(?:attend|sign)/i.test(
    message
  );
}

function normalizeCookie(value) {
  if (typeof value !== "string") {
    throw new Error("账号 Cookie 必须是字符串");
  }
  const cookie = value.replace(/^cookie\s*:\s*/i, "").trim();
  if (!cookie) throw new Error("账号 Cookie 为空");
  if (/[\r\n]/.test(cookie)) {
    throw new Error("单个 Cookie 中不能包含换行；多账号请使用 JSON 数组");
  }
  if (!cookie.includes("=")) {
    throw new Error("Cookie 格式无效，应为 name=value; name2=value2");
  }
  return cookie;
}

function normalizeUserAgent(value) {
  if (typeof value !== "string" || !value.trim()) return DEFAULT_USER_AGENT;
  const userAgent = value.trim();
  if (/[\r\n]/.test(userAgent)) {
    throw new Error("User-Agent 不能包含换行");
  }
  return userAgent;
}

function parseMaybeJson(raw, variableName) {
  const trimmed = raw.trim();
  if (!trimmed.startsWith("[") && !trimmed.startsWith("{")) return null;
  try {
    return JSON.parse(trimmed);
  } catch {
    throw new Error(`${variableName} 看起来是 JSON，但格式不正确`);
  }
}

function accountItemsFromJson(value) {
  if (Array.isArray(value)) return value;
  if (!value || typeof value !== "object") return [value];
  if (Array.isArray(value.accounts)) return value.accounts;
  if (typeof value.cookie === "string" || typeof value.token === "string") {
    return [value];
  }

  // 兼容 {"账号一":"session=...","账号二":"session=..."} 形式。
  return Object.entries(value).map(([name, cookie]) => ({ name, cookie }));
}

function parseUserAgents(raw) {
  if (!raw.trim()) return { fallback: DEFAULT_USER_AGENT, list: [], byName: {} };
  const parsed = parseMaybeJson(raw, "NODESEEK_USER_AGENT");
  if (parsed === null) {
    const lines = raw
      .split(/\r?\n/)
      .map((item) => item.trim())
      .filter(Boolean);
    return {
      fallback: normalizeUserAgent(lines[0]),
      list: lines.map(normalizeUserAgent),
      byName: {},
    };
  }
  if (Array.isArray(parsed)) {
    const list = parsed.map((item) => normalizeUserAgent(String(item || "")));
    return {
      fallback: list[0] || DEFAULT_USER_AGENT,
      list,
      byName: {},
    };
  }
  if (typeof parsed === "object" && parsed) {
    const byName = Object.fromEntries(
      Object.entries(parsed).map(([name, value]) => [
        name,
        normalizeUserAgent(String(value || "")),
      ])
    );
    return {
      fallback: Object.values(byName)[0] || DEFAULT_USER_AGENT,
      list: [],
      byName,
    };
  }
  throw new Error("NODESEEK_USER_AGENT 格式无效");
}

function parseAccounts(rawCookie, rawUserAgent) {
  const parsed = parseMaybeJson(rawCookie, "NODESEEK_COOKIE");
  const items =
    parsed === null
      ? rawCookie
          .split(/\r?\n/)
          .map((item) => item.trim())
          .filter(Boolean)
      : accountItemsFromJson(parsed);
  const userAgents = parseUserAgents(rawUserAgent);

  return items.map((item, index) => {
    const account = typeof item === "string" ? { cookie: item } : item;
    if (!account || typeof account !== "object") {
      throw new Error(`账号 ${index + 1} 的配置格式无效`);
    }
    const name = cleanText(
      account.userName || account.name || account.username || "",
      40
    );
    const rawUserId = account.userId ?? account.uid ?? account.memberId;
    const userId =
      rawUserId !== undefined && /^\d+$/.test(String(rawUserId).trim())
        ? String(rawUserId).trim()
        : "";
    const configuredUserAgent =
      account.userAgent ||
      account.ua ||
      (name && userAgents.byName[name]) ||
      userAgents.list[index] ||
      userAgents.fallback;
    return {
      name,
      userId,
      cookie: normalizeCookie(account.token ?? account.cookie),
      userAgent: normalizeUserAgent(configuredUserAgent),
    };
  });
}

function randomModeEnabled() {
  const raw = String(
    process.env.NODESEEK_RANDOM || process.env.NODESEEK_SIGN_MODE || "false"
  )
    .trim()
    .toLowerCase();
  if (["false", "0", "no", "fixed", "fixed5", "5"].includes(raw)) {
    return false;
  }
  if (["true", "1", "yes", "random", "lucky"].includes(raw)) {
    return true;
  }
  throw new Error("NODESEEK_RANDOM 只能填写 true（试试手气）或 false（固定 5 个）");
}

function resultMessage(data) {
  if (!data || typeof data !== "object") return cleanText(data);
  return cleanText(
    data.message ??
      data.msg ??
      data.error ??
      data.data?.message ??
      data.data?.msg ??
      ""
  );
}

function resultSucceeded(data) {
  if (!data || typeof data !== "object") return false;
  if ([true, 1, "1", "true"].includes(data.success)) return true;
  if (data.success === false || data.success === 0 || data.success === "false") {
    return false;
  }
  return [0, 200, "0", "200"].includes(data.code);
}

function requestWithBrowserFingerprint(requestPath, method, account) {
  const helperPath = path.join(__dirname, "nodeseek_http.py");
  const payload = JSON.stringify({
    path: requestPath,
    method,
    cookie: account.cookie,
    timeout: Math.ceil(REQUEST_TIMEOUT_MS / 1000),
  });

  return new Promise((resolve, reject) => {
    const child = execFile(
      "python3",
      [helperPath],
      { encoding: "utf8", maxBuffer: 2 * 1024 * 1024, timeout: 180000 },
      (error, stdout, stderr) => {
        let result;
        try {
          result = JSON.parse(stdout);
        } catch {
          const detail = cleanText(stderr || error?.message, 180);
          reject(
            new Error(
              `浏览器指纹请求组件运行失败${detail ? `：${detail}` : ""}`
            )
          );
          return;
        }

        if (result.error) {
          reject(new Error(result.message || "浏览器指纹请求组件运行失败"));
          return;
        }
        resolve(result);
      }
    );
    // Cookie 仅通过标准输入传递，不出现在命令行参数或任务日志中。
    child.stdin.on("error", () => {});
    child.stdin.end(payload);
  });
}

async function requestJson(path, account, options = {}) {
  const method = options.method || "GET";
  const response = await requestWithBrowserFingerprint(path, method, account);
  if (response.cloudflareChallenge) {
    throw new CloudflareChallengeError(
      `青龙出口被 Cloudflare 挑战（已尝试 ${response.attempts || 1} 种浏览器指纹）；这不是账号 Cookie 过期`
    );
  }
  const text = response.text;
  const redirectedToLogin = /(?:signIn|login)/i.test(response.url);
  if (response.status === 401 || redirectedToLogin) {
    throw new AccountExpiredError("账号数据已过期，请重新获取账号信息");
  }

  let data;
  try {
    data = JSON.parse(text);
  } catch {
    const preview = cleanText(text, 120);
    if (looksLikeExpired(preview)) {
      throw new AccountExpiredError("账号数据已过期，请重新获取账号信息");
    }
    throw new Error(
      `接口返回的不是 JSON（HTTP ${response.status}${
        preview ? `：${preview}` : ""
      }；指纹 ${response.impersonate || "未知"}${
        response.location ? `，重定向 ${cleanText(response.location, 100)}` : ""
      }）`
    );
  }

  const message = resultMessage(data);
  const authCode = Number(data?.status ?? data?.code ?? data?.data?.status);
  if (
    looksLikeExpired(message) ||
    [401, 404, 1001, 4001].includes(authCode)
  ) {
    throw new AccountExpiredError(
      `账号数据已过期：${message || "请重新获取账号信息"}`
    );
  }
  // NodeSeek 会以 HTTP 500 返回“今天已完成签到”，但这属于正常业务状态。
  if (looksLikeAlreadySigned(message)) return data;
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}${message ? `：${message}` : ""}`);
  }
  return data;
}

function accountLabel(account, index, total) {
  if (account.name) return account.name;
  return total > 1 ? `账号 ${index + 1}` : "账号";
}

function boardSummary(board) {
  if (!board || typeof board !== "object") return "";
  const parts = [];
  if (board.record && typeof board.record === "object") {
    const gain = board.record.gain ?? board.record.coin ?? board.record.current;
    if (gain !== undefined && gain !== null) parts.push(`今日获得 ${gain} 个鸡腿`);
  }
  if (board.order !== undefined && board.order !== null) {
    parts.push(`当前排名第 ${board.order}`);
  }
  return parts.join("，");
}

async function signOne(account, index, total, useRandom) {
  const fallbackLabel = accountLabel(account, index, total);
  const signResult = await requestJson(
    `/api/attendance?random=${useRandom ? "true" : "false"}`,
    account,
    { method: "POST" }
  );
  const message = resultMessage(signResult) || "签到接口未返回说明";
  const alreadySigned = looksLikeAlreadySigned(message);
  const success = resultSucceeded(signResult) || alreadySigned;
  if (!success) {
    throw new Error(`签到失败：${message}`);
  }
  if (alreadySigned) {
    const summary = `${fallbackLabel}：${message}`;
    console.log(summary);
    return summary;
  }

  let displayName = account.name;
  let coin;
  if (account.userId) {
    try {
      const info = await requestJson(
        `/api/account/getInfo/${encodeURIComponent(account.userId)}?readme=1`,
        account
      );
      if (!resultSucceeded(info)) {
        throw new Error(resultMessage(info) || "接口未返回用户资料");
      }
      displayName = cleanText(
        info.detail?.member_name || info.detail?.userName || displayName,
        40
      );
      coin = info.detail?.coin ?? info.detail?.coins;
    } catch (error) {
      console.warn(`${fallbackLabel}用户资料查询失败：${error.message}`);
    }
  }

  const label = displayName || fallbackLabel;
  let boardText = "";
  try {
    const board = await requestJson("/api/attendance/board?page=1", account);
    boardText = boardSummary(board);
  } catch (error) {
    if (error instanceof AccountExpiredError) throw error;
    console.warn(`${label}排行榜查询失败：${error.message}`);
    boardText = "排行榜查询失败，但签到接口已成功";
  }

  const current = signResult.current ?? signResult.data?.current;
  const details = [message];
  const currentCoin = coin ?? current;
  if (currentCoin !== undefined && currentCoin !== null) {
    details.push(`当前鸡腿 ${currentCoin} 个`);
  }
  if (boardText) details.push(boardText);
  const summary = `${label}：${details.join("；")}`;
  console.log(summary);
  return summary;
}

async function main() {
  const rawCookie =
    process.env.NODESEEK_DATA ||
    process.env.nodeseek_data ||
    process.env.NODESEEK_COOKIE ||
    process.env.NODESEEK_COOKIES ||
    process.env.NS_COOKIE ||
    "";
  if (!rawCookie.trim()) {
    throw new Error(
      "未配置 NODESEEK_DATA，请先用浏览器插件获取并上传 NodeSeek 账号信息。"
    );
  }

  const accounts = parseAccounts(
    rawCookie,
    process.env.NODESEEK_USER_AGENT || process.env.NODESEEK_UA || ""
  );
  if (!accounts.length) {
    throw new Error("NODESEEK_DATA 中没有可用账号");
  }
  const useRandom = randomModeEnabled();
  console.log(
    `NodeSeek 签到开始，共 ${accounts.length} 个账号，模式：${
      useRandom ? "试试手气" : "固定鸡腿 x 5"
    }`
  );

  let failures = 0;
  let expired = 0;
  const summaries = [];
  for (const [index, account] of accounts.entries()) {
    const label = accountLabel(account, index, accounts.length);
    try {
      const summary = await signOne(
        account,
        index,
        accounts.length,
        useRandom
      );
      summaries.push(`✅ ${summary}`);
    } catch (error) {
      failures += 1;
      if (error instanceof AccountExpiredError) expired += 1;
      const summary = `${label}：${error.message}`;
      console.error(summary);
      summaries.push(`❌ ${summary}`);
    }
  }

  const title =
    expired === accounts.length
      ? "NodeSeek账号数据已过期"
      : failures
        ? "NodeSeek签到异常"
        : "NodeSeek签到完成";
  await pushFromQingLong(title, summaries.join("\n"));
  if (failures) process.exitCode = 1;
}

main().catch(async (error) => {
  console.error(`NodeSeek 签到失败：${error.message}`);
  await pushFromQingLong("NodeSeek账号数据异常", error.message);
  process.exitCode = 1;
});
