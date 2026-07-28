# 青龙签到脚本

本仓库提供两个青龙签到任务：

- `ithome_ql.js`：IT之家签到，读取 `ITHOME_USER_HASH`。
- `nodeseek_ql.js`：NodeSeek 签到，读取 `NODESEEK_DATA`，并调用同目录的 `nodeseek_http.py`。

NodeSeek 运行前需在青龙中安装 Python 依赖：

```text
curl_cffi==0.15.0
```

脚本通知统一调用青龙系统通知；账号数据由浏览器扩展写入青龙环境变量。
