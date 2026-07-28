/**
 * IT之家青龙签到
 * 环境变量：ITHOME_USER_HASH（多账号可用换行或 & 分隔）
 * 建议定时：10 8 * * *
 */

"use strict";

const crypto = require("crypto");

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

const USER_AGENT_SIGN =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 14_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 ithome/rmsdklevel2/day/7.63";
const USER_AGENT_INFO =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 ithome/rmsdklevel2/day/7.32";
const SKEY =
  "hd7%b4f8p9)*fd4h5l6|)123/*-+!#$@%^*()_+?>?njidfds[]rfbcvnb3rz/ird|opqqyh487874515/%90hggigadfihklhkopjj`b3hsdfdsf84215456fi15451%q(#@Fzd795hn^Ccl$vK^L%#w$^yr%ETvX#0TaPSRm5)OeG)^fQnn6^%^UTtJI#3EZ@p6^Rf$^!O$(jnkOiBjn3#inhOQQ!aTX8R)9O%#o3zCVxo3tLyVorwYwA^$%^b9Yy$opSEAOOlFBsS^5d^HoF%tJ$dx%3)^q^c^$al%b4I)QHq^#^AlcK^KZFYf81#bL$n@$%j^H(%m^";

function pad2(value) {
  return String(value).padStart(2, "0");
}

function formatLocalDate(timestamp) {
  const date = new Date(timestamp);
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(
    date.getDate()
  )} ${pad2(date.getHours())}:${pad2(date.getMinutes())}:${pad2(
    date.getSeconds()
  )}`;
}

function getDynamicKey(length, timestamp) {
  const today = new Date(timestamp).getDate();
  const value = Math.round(timestamp / 50000) * today * 3;
  const divisors =
    length === 3
      ? [1000, 100, 10]
      : [10000000, 1000000, 100000, 10000, 1000, 100, 10, 1];

  return divisors
    .map((divisor) => {
      const digit = Math.trunc((value % (divisor * 10)) / divisor);
      return SKEY[digit * today];
    })
    .join("");
}

function tripleDesHex(plainText, shortKey) {
  const input = Buffer.from(plainText, "utf8");
  const paddedLength = Math.ceil(input.length / 8) * 8;
  const padded = Buffer.alloc(paddedLength);
  input.copy(padded);

  const keyPart = Buffer.from(shortKey, "utf8");
  const key = Buffer.concat([keyPart, keyPart, keyPart]);
  const cipher = crypto.createCipheriv("des-ede3-ecb", key, null);
  cipher.setAutoPadding(false);
  return Buffer.concat([cipher.update(padded), cipher.final()]).toString("hex");
}

function buildSignUrl(userHash, timestamp) {
  const key = getDynamicKey(8, timestamp);
  const parameterName = `k${tripleDesHex(
    getDynamicKey(3, timestamp),
    key
  )}`;
  const parameterValue = tripleDesHex(formatLocalDate(timestamp), key);
  const url = new URL("https://napi.ithome.com/api/usersign/sign");
  url.searchParams.set("userHash", userHash);
  url.searchParams.set("type", "0");
  url.searchParams.set("timestamp", String(timestamp));
  url.searchParams.set(parameterName, parameterValue);
  return url;
}

async function requestJson(url, userAgent) {
  const response = await fetch(url, {
    headers: { "User-Agent": userAgent },
    signal: AbortSignal.timeout(20000),
  });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${text.slice(0, 160)}`);
  }
  try {
    return JSON.parse(text);
  } catch {
    throw new Error(`接口返回的不是 JSON：${text.slice(0, 160)}`);
  }
}

async function signOne(userHash, index, total) {
  const label = total > 1 ? `账号 ${index + 1}` : "账号";
  const timestamp = Date.now();
  const signResult = await requestJson(
    buildSignUrl(userHash, timestamp),
    USER_AGENT_SIGN
  );
  const signText =
    signResult.ok === 0
      ? signResult.msg
      : signResult.ok === 1
        ? signResult.title
        : `未知结果：${JSON.stringify(signResult)}`;

  if (/请登录|未登录|登录.*失效|账号.*过期/i.test(signText)) {
    throw new Error(`账号数据已过期：${signText}`);
  }

  const infoUrl = new URL(
    "https://napi.ithome.com/api/usersign/getsigninfo"
  );
  infoUrl.searchParams.set("userHash", userHash);
  const info = await requestJson(infoUrl, USER_AGENT_INFO);
  const summary = `${label}：${signText}；连续 ${
    info.cdays ?? "-"
  } 天，累计 ${info.mdays ?? "-"} 天，金币 ${info.totalcoin ?? "-"} 个`;
  console.log(summary);
  return summary;
}

async function main() {
  const raw =
    process.env.ITHOME_USER_HASH || process.env.senku_ithome_userHash || "";
  const accounts = raw
    .split(/[&\n]/)
    .map((value) => value.trim())
    .filter(Boolean);

  if (!accounts.length) {
    throw new Error(
      "未配置 ITHOME_USER_HASH。请填入 IT之家请求链接中的 userHash（不是整段 Cookie）。"
    );
  }

  console.log(`IT之家签到开始，共 ${accounts.length} 个账号`);
  let failures = 0;
  const summaries = [];
  for (const [index, userHash] of accounts.entries()) {
    try {
      const summary = await signOne(userHash, index, accounts.length);
      summaries.push(`✅ ${summary}`);
    } catch (error) {
      failures += 1;
      const summary = `${
        accounts.length > 1 ? `账号 ${index + 1}` : "账号"
      }签到失败：${error.message}`;
      console.error(summary);
      summaries.push(`❌ ${summary}`);
    }
  }

  await pushFromQingLong(
    failures ? "IT之家签到异常" : "IT之家签到完成",
    summaries.join("\n")
  );
  if (failures) process.exitCode = 1;
}

main().catch(async (error) => {
  console.error(`IT之家签到失败：${error.message}`);
  await pushFromQingLong("IT之家账号数据异常", error.message);
  process.exitCode = 1;
});
