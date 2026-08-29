# 青龙签到脚本

本仓库提供两个青龙签到任务：

- `ithome_ql.js`：IT之家签到，读取 `ITHOME_USER_HASH`。
- `nodeseek_ql.js`：NodeSeek 签到，读取 `NODESEEK_DATA`，并调用同目录的 `nodeseek_http.py`。

NodeSeek 运行前需在青龙中安装 Python 依赖：

```text
curl_cffi==0.15.0
```

## NodeSeek Cloudflare 后备

NodeSeek 对数据中心出口启用 Managed Challenge 时，仅轮换 User-Agent 或 TLS 指纹无法通过。`nodeseek_http.py` 平时继续使用轻量的 `curl_cffi`；遇到挑战后，将目标 API 请求交给青龙同一 Docker 网络中的真实 Camoufox 浏览器完成。挑战、延迟生成的 `fog` 工作量证明和签到发生在同一个浏览器上下文，避免跨 IP/指纹重放验证 Cookie 时再次被拒绝。签到成功后的账号资料与排行榜也在这一个浏览器会话中预取，不会重复挑战。

浏览器服务默认地址为 `http://nodeseek-browser:8191/nodeseek`，可用 `NODESEEK_BROWSER_URL` 覆盖。登录 Cookie 只通过青龙内部 Docker 网络的请求体传递，不会出现在命令行、任务输出或容器日志中。后备 API 只允许 NodeSeek 的签到、账号资料和排行榜三个固定路径，不能充当通用代理。

先在仓库目录构建受限后备镜像：

```sh
docker build -f Dockerfile.nodeseek-browser -t nodeseek-browser:1.0.0 .
```

再以仅容器内可访问的方式运行：

```sh
docker run -d \
  --name nodeseek-browser \
  --restart unless-stopped \
  --network 1panel-network \
  --shm-size 512m \
  --memory 1g \
  nodeseek-browser:1.0.0
```

不要为该服务映射公网端口。

脚本通知统一调用青龙系统通知；账号数据由浏览器扩展写入青龙环境变量。
