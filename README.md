# Steam 在线状态与游戏时数监控

抓取配置的 Steam 个人主页，记录上下线状态及主页最近游戏的总时数变化。请求使用中文 Accept-Language、Steam_Language=schinese Cookie 和 l=schinese 参数，不需要登录 Cookie。

## 运行规则

- 默认账号：https://steamcommunity.com/profiles/xxx
- 在线状态默认每 60 秒查询一次；游戏时数独立每 300 秒查询一次，不依赖账号在线状态。
- 默认时区为 Asia/Shanghai（北京时间，+08:00）。
- 时间是程序观察到数据的时间，不是 Steam 提供的精确上下线/游玩时间。Steam 页面可能延迟更新、四舍五入显示时数；隐身或离线时也可能出现时数更新。
- 只抓取主页展示的最近游戏，不覆盖整个游戏库。游戏详情不可见时无法获取对应信息。

## HTTP 接口总览

默认监听 `0.0.0.0:80`。以下示例在服务器本机执行，远程访问时将 `127.0.0.1` 换为服务器地址。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | /healthz | HTTP 服务存活检查 |
| GET | /status | 最近状态采样数组 |
| GET | /today | 今日状态分段 |
| GET | /daily-status | 按天配对的上下线记录 |
| GET | /late-offline | 晚于指定时间下线的日期 |
| GET | /game-stats | 最近一次成功抓取的游戏数据 |
| GET | /game-stats/changes | 游戏总时数变化历史数组 |
| GET | /readme | 本文 Markdown 原文 |
| GET | /polling | 全部抓取的开关及在途状态 |
| POST | /polling | 暂停/恢复全部抓取，需 Bearer 鉴权 |

除 `/readme` 返回 UTF-8 Markdown 外，其余接口返回 UTF-8 JSON。不支持的路径返回 HTTP 404 和 `{"error":"not_found"}`。GET 查询仍无身份认证，请勿在不希望公开账号活动记录的情况下直接开放到公网；可通过防火墙限制来源或在反向代理增加认证。POST 控制接口必须配置 `CONTROL_TOKEN`，远程使用应通过 HTTPS 反向代理或 SSH 隧道，避免明文传输 Bearer token；不要把 token 放到 URL、源码或日志中。

### 1. GET /healthz

```bash
curl "http://127.0.0.1/healthz"
```

```json
{"ok": true}
```

仅说明 HTTP 服务正常响应，不代表 Steam 抓取成功。

### 2. GET /status

返回内存中的最近状态采样，服务重启会清空。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| minutes | 15 | 最近多少分钟，范围 1–1440；实际受内存保留窗口限制 |
| order | desc | asc 为时间正序，其他值均按倒序返回 |

```bash
curl "http://127.0.0.1/status?minutes=30&order=desc"
```

```json
[
  {"ts":"2026-11-03T21:10:00+08:00","status":"offline","raw_text":"当前离线"},
  {"ts":"2026-11-03T21:09:00+08:00","status":"online","raw_text":"当前在线"}
]
```

`status` 为 `online`、`offline` 或 `unknown`；`raw_text` 是页面状态文本，无法取得时可为 null。无数据返回 `[]`。

### 3. GET /today

返回本进程内存中今日的状态分段，不从历史日志恢复。无参数。

```bash
curl "http://127.0.0.1/today"
```

```json
[
  {"day":"2026-11-03","status":"online","start_ts":"2026-11-03T20:00:00+08:00","end_ts":"2026-11-03T21:10:00+08:00"},
  {"day":"2026-11-03","status":"offline","start_ts":"2026-11-03T21:10:00+08:00","end_ts":null}
]
```

`end_ts: null` 表示当前尚未结束的分段；进程启动时的首次观测不代表真实上线/下线起点。

### 4. GET /daily-status

读取状态日志及轮转文件中的明确 `offline → online`、`online → offline` 切换，按时间顺序配对。日期倒序，每天的会话按时间正序。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| date | 不筛选 | 指定 YYYY-MM-DD 日期；非法日期返回 HTTP 400 |
| limit | 0 | 最多返回多少个有记录的日期；0 不限制，最大 1000 |

```bash
curl "http://127.0.0.1/daily-status?limit=30"
curl "http://127.0.0.1/daily-status?date=2026-11-03"
```

```json
[
  {
    "date":"2026-11-03",
    "weekday":"星期二",
    "sessions":[
      {"online_time":"2026-11-03T09:00:00+08:00","offline_time":"2026-11-03T12:00:00+08:00"},
      {"online_time":"2026-11-03T23:00:00+08:00","offline_time":"2026-11-04T01:00:00+08:00"}
    ]
  }
]
```

- 跨午夜会话归入上线日期，筛选该日期仍包含下一天的下线时间。
- 缺少上线记录：`online_time` 为 null，按下线日期归组。
- 未记录到下线：`offline_time` 为 null，不代表仍然在线。
- 初次观测不会冒充状态切换；未知状态切换不用于配对。日志缺失或停机可能导致不完整记录，不能视为精确游戏会话。
- 无记录返回 `[]`，不会补出无记录的日期。

### 5. GET /late-offline

扫描状态日志及轮转文件，返回严格晚于阈值的 `online → offline` 切换，按下线发生日期分组，日期倒序。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| after | 20:30 | 时间阈值，HH:MM，恰好等于阈值不计入；无效值回退到 20:30 |
| limit | 0 | 最多返回日期数；0 不限制，最大 1000 |

```bash
curl "http://127.0.0.1/late-offline?after=21:00&limit=30"
```

```json
[
  {"date":"2026-11-03","weekday":"星期二","offline_times":["2026-11-03T21:10:00+08:00"]}
]
```

按自然日的钟点比较，不把凌晨下线算作前一天晚上；每天的 `offline_times` 不保证时间排序。

### 6. GET /game-stats

返回最近一次成功查询得到的主页最近游戏，无参数。

```bash
curl "http://127.0.0.1/game-stats"
```

```json
{
  "profile_url":"https://steamcommunity.com/profiles/76561198406118342",
  "poll_interval_s":300,
  "last_success_at":"2026-11-03T21:10:00+08:00",
  "error":null,
  "games":[
    {"appid":"2663530","name":"POOLS","total_hours":"2.7","last_played_text":"目前正在游戏"}
  ]
}
```

- `total_hours`：精确十进制字符串，避免浮点比较误差。
- `last_played_text`：页面文本，也可能是“最后运行日期：10 月 30 日”或 null，不推测精确时间。
- `last_success_at`：最近成功抓取并处理数据的时间，进程首次成功抓取前为 null。
- `error`：最近失败的错误摘要；成功后清空。失败保留上次成功快照，请结合时间判断是否过期。
- 重启后恢复历史基准，但当前快照需等待重新抓取。游戏模块未启用返回 HTTP 503 和 `{"error":"game_stats_unavailable"}`。

### 7. GET /game-stats/changes

读取持久化日志，返回总时数变化数组，最新记录在前。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| limit | 100 | 返回最近记录数，范围 1–1000；0 会被限制为 1，不表示全部 |

```bash
curl "http://127.0.0.1/game-stats/changes?limit=100"
```

```json
[
  {
    "event":"change",
    "profile_url":"https://steamcommunity.com/profiles/76561198406118342",
    "appid":"2663530",
    "name":"POOLS",
    "total_hours":"2.7",
    "last_played_text":"目前正在游戏",
    "old_total_hours":"2.5",
    "observed_at":"2026-11-03T21:10:00+08:00"
  }
]
```

- 每 5 分钟独立查询，在线/离线均检查；总时数变化才记录（增加或减少都记录）。
- `observed_at` 是发现变化的查询时间，不是实际游戏起止时间。
- 首次发现游戏只写 baseline，不出现在变化数组中；没有变化返回 `[]`。
- 仅最后运行文本变化不会新增记录；时数不变的轮询也不会保存历史点。
- 游戏从主页消失不按零处理，不删除基准；重启从日志恢复基准，防止重复记录。
- 游戏模块未启用返回 HTTP 503。

### 8. GET /readme

返回本 README 的原始 Markdown，`Content-Type: text/markdown; charset=utf-8`，不是 JSON，也不是渲染后的 HTML。

```bash
curl "http://127.0.0.1/readme"
curl "http://127.0.0.1/readme" -o README.md
```

读取固定的项目 README，不接受文件路径参数。文件缺失或不可读时返回 HTTP 503 和 `{"error":"readme_unavailable"}`。

### 9. GET /polling 与 POST /polling

开关只存在于当前进程内存，初始及每次重启均默认开启，不持久化。同时控制在线状态和游戏时数两条抓取路径，不重启服务，不清空采样、游戏快照、基准或日志，不影响原有查询接口。

```bash
curl "http://127.0.0.1/polling"
# CONTROL_TOKEN 已通过安全渠道注入当前 shell，值应与服务端一致
curl -X POST "http://127.0.0.1/polling" \
  -H "Authorization: Bearer ${CONTROL_TOKEN}" \
  -H "Content-Type: application/json" -d '{"enabled":false}'
# 恢复
curl -X POST "http://127.0.0.1/polling" \
  -H "Authorization: Bearer ${CONTROL_TOKEN}" \
  -H "Content-Type: application/json" -d '{"enabled":true}'
```

GET 与成功 POST 返回相同结构（不包含 token）：

```json
{"enabled":false,"in_flight":{"status":0,"games":0}}
```

- `enabled: false` 阻止新的抓取轮次准入；暂停前已获准的轮次属于在途，允许完成请求、解析和写入，因此返回暂停成功不代表所有请求已结束。`in_flight` 分别计数状态/游戏在途轮次，均为 0 时才完全静默；在途请求可能包括重定向。暂停不取消这些请求。
- 暂停使用条件变量等待，不忙循环；恢复唤醒原有线程，不创建新线程。状态仍按每轮完成后等待默认 60 秒（可配置），游戏仍按每轮开始间隔 300 秒调度。恢复时未到期则继续等待，到期或暂停期间已到期则立即抓取一次，不补抓遗漏轮次，不因为重复发送 `true` 提前抓取。
- 暂停本身不删除数据；查询仍遵循原有时间窗口和保留规则，长时间暂停后 `/status` 可能没有窗口内样本。历史分段不自动补断点，暂停期间的真实上下线/游玩变化无法观察。
- 这是手动冷却开关，不是 429 自动退避、自动解除或自动恢复机制，也不能保证恢复时 Steam 已解除限流。
- POST 必须为 UTF-8 `application/json`（可带 `charset=utf-8`），带 `Content-Length`，正文最多 1024 字节，只接受 `{"enabled":true}` 或 `{"enabled":false}`。字符串、数字、null、数组、缺少字段和额外字段均返回 400；坏 JSON、缺失/非法长度及不支持的分块传输也返回 400；超长返回 413；不支持的 Content-Type/字符集返回 415。
- 服务启动时读取 `CONTROL_TOKEN`；未配置或空白时 POST 返回 503，不影响默认抓取和 GET。缺少/错误 Bearer token 返回 401。必须先通过鉴权才校验正文。修改环境变量需要重启服务；开关切换本身不需要重启。建议使用安全渠道提供的高强度随机 token，并限制配置文件权限。

PowerShell 调用示例（服务与当前终端均已配置同一 `$env:CONTROL_TOKEN`）：

```powershell
Invoke-RestMethod http://127.0.0.1/polling
Invoke-RestMethod http://127.0.0.1/polling -Method Post -Headers @{Authorization="Bearer $env:CONTROL_TOKEN"} -ContentType 'application/json' -Body '{"enabled":false}'
Invoke-RestMethod http://127.0.0.1/polling -Method Post -Headers @{Authorization="Bearer $env:CONTROL_TOKEN"} -ContentType 'application/json' -Body '{"enabled":true}'
```

### 参数及错误约定

数值参数不是有效整数时使用默认值；低于最小值/高于最大值时截到边界。`date` 非空且格式无效时返回 400；空查询参数通常按未传处理。无匹配数据通常返回空数组，不是错误。示例数据仅用于说明结构。

## 日志与持久化

| 数据 | 默认位置 | 重启后 |
| --- | --- | --- |
| 状态采样与状态切换日志 | logs/steam_online_checker.log 及轮转文件 | 可供 daily-status、late-offline 查询 |
| 游戏基准与变化历史 | 与 LOG_FILE 同目录的 game_stats.jsonl | 恢复基准并可查询变化历史 |
| status、today、游戏当前快照 | 进程内存 | 重新积累或抓取 |

状态日志默认每个文件最大 5 MiB，保留 3 个备份，超出后旧日志会被轮转淘汰，因此不能保证永久查询所有上下线历史。游戏日志不自动轮转，需关注磁盘容量并自行备份。不要在运行期间手工改写游戏日志。

使用下方 Docker 挂载后，宿主机文件为：

- `/var/log/steam-online-checker/steam_online_checker.log`
- `/var/log/steam-online-checker/game_stats.jsonl`

删除/重建容器不会删除挂载目录中的日志；宿主机磁盘故障、手工删除和日志轮转仍可能导致数据丢失。

## 本地运行（Windows PowerShell）

若需要 HTTP 热开关，启动 Python 前请通过安全渠道将 token 注入 `$env:CONTROL_TOKEN`；不配置仍能运行和查询，但 POST 控制返回 503。不要将真实 token 写入源码或提交到版本库。

```powershell
cd steam_online_checker
python -m venv .venv
.\.venv\Scripts\pip.exe install -r requirements.txt
.\.venv\Scripts\python.exe -m app.main --interval 60
```

单次抓取，不开启 HTTP：

```powershell
.\.venv\Scripts\python.exe -m app.main --once
```

`--no-http` 为持续抓取但不启动 HTTP。

## Docker 部署与更新（Ubuntu）

先上传更新后的源码、README 和 Dockerfile，再在服务器执行。重建镜像本身不会更新已运行的容器，必须重建容器。

```bash
cd /home/ubuntu/steam_online_checker
docker build -t steam-online-checker:latest .
```

确认构建成功后，停止并删除已有容器（首次部署跳过）：

```bash
docker stop steam-online-checker
docker rm steam-online-checker
```

启动新容器：先通过安全渠道在宿主机 shell 导出 `CONTROL_TOKEN`，下方 `-e CONTROL_TOKEN` 将其传入容器（不在命令中写死密钥）；只有配置非空 token 才能使用 POST 热开关。未配置时仍可抓取和查询，但写操作返回 503。Docker 管理权限可读取容器环境，请限制宿主机访问权限。

```bash
docker run -d \
  --name steam-online-checker \
  --restart unless-stopped \
  -p 80:80 \
  -e STEAM_PROFILE_URL="https://steamcommunity.com/profiles/76561198406118342" \
  -e INTERVAL_S=60 \
  -e CONTROL_TOKEN \
  -e TIMEZONE="Asia/Shanghai" \
  -v /var/log/steam-online-checker:/app/logs \
  steam-online-checker:latest
```

验证：

```bash
curl "http://127.0.0.1/healthz"
curl "http://127.0.0.1/readme"
curl "http://127.0.0.1/game-stats"
docker logs --tail 50 steam-online-checker
```

远程访问需放行相应端口；GET 查询仍无认证，POST 热开关有 Bearer 鉴权。公网控制请使用 HTTPS 或 SSH 隧道，不要直接明文传输 token。

## 常用配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| STEAM_PROFILE_URL | 上述账号主页 | 监控目标 |
| INTERVAL_S | 60 | 在线状态轮询间隔，不影响游戏的固定 300 秒间隔 |
| TIMEOUT_S | 20 | 抓取请求超时秒数 |
| TIMEZONE | Asia/Shanghai | 记录和统计使用的时区 |
| HTTP_HOST | 0.0.0.0 | HTTP 监听地址 |
| CONTROL_TOKEN | 未配置 | POST /polling 的 Bearer token；为空禁用写操作，启动时读取，GET 不返回 |
| PORT / HTTP_PORT | 80 | HTTP 端口，PORT 优先；修改后需相应调整 Docker 端口映射 |
| LOG_FILE | logs/steam_online_checker.log | Docker 镜像中为 /app/logs/steam_online_checker.log |
| LOG_MAX_BYTES | 5242880 | 状态日志轮转大小 |
| LOG_BACKUP_COUNT | 3 | 状态日志备份数量 |
| RETENTION_MINUTES | 1440 | 状态内存保留窗口 |
| LOG_LEVEL | INFO | 日志级别；高于 INFO 会影响状态日志历史记录 |
