# Torrent Webhook 分发系统

接收来自 autobrr / Radarr / Sonarr 等工具的 Webhook，自动将种子推送到负载最优的 qBittorrent 节点。支持通过 Vertex 代理访问 qB，并可发送 Telegram 通知。

---

## 模块说明

### `torrent_webhook.py`
Flask Webhook 服务主程序。

- 接收 Webhook POST 请求（包含种子名称、分类、下载链接）
- 并发探测所有 qB 节点状态（上传速度 / 种子数 / 剩余空间）
- 按策略选出最优节点后推送种子
- 推送失败时自动重试，并尝试刷新 Cookie
- 通过 Telegram Bot 发送成功/失败通知
- 提供管理面板 `/admin`，支持查看节点状态、修改 `.env`、查看日志

### `vertex_cookie.py`
Vertex 登录 Cookie 自动管理模块。

- 自动 MD5 加密明文密码后登录 Vertex
- Cookie 有效期内直接复用，过期自动刷新
- 结果缓存到本地 `vertex_cookie_cache.json`
- 支持多账号/多节点（以 URL + 用户名为缓存 Key）
- 可通过环境变量构造实例

---

## 快速开始

**1. 安装依赖**
```bash
pip install flask python-dotenv requests
```

**2. 配置 `.env`**
```env
# ── 运行模式：使用 Vertex 代理（推荐）或直连 qBittorrent ──
USE_VT_MODE=true

# ── Vertex 配置（USE_VT_MODE=true 时填写）──
VTURL=http://your-vertex-host:3077
VT_USERNAME=admin
VT_PASSWORD=your-plaintext-password   # 程序自动 MD5，也可直接填 MD5
VT_PROXY_IDS=proxy_id_1,proxy_id_2    # Vertex 中的 proxy id，逗号分隔

# ── 直连 qBittorrent（USE_VT_MODE=false 时填写）──
# QB_SERVERS=http://host1:8080,http://host2:8080
# QB_USERNAMES=admin,admin
# QB_PASSWORDS=pass1,pass2

# ── 选择策略 ──
SELECT_STRATEGY=upload_speed   # upload_speed | torrent_count | free_space | all

# ── Webhook 路径 ──
WEBHOOK_PATH=webhook/your-secret-path

# ── Telegram 通知（可选）──
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# ── 其他 ──
FLASK_PORT=5000
ADMIN_SECRET=your-admin-secret   # 管理面板鉴权，留空则不验证
```

**3. 启动服务**
```bash
python torrent_webhook.py
```

---

## Webhook 格式

```http
POST /webhook/your-secret-path
Content-Type: application/json

{
  "release_name": "Movie.Name.2024.1080p",
  "indexer": "tracker-name",
  "download_url": "https://..."
}
```

---

## 选择策略

| 策略 | 说明 |
|------|------|
| `upload_speed` | 选当前上传速度最低的节点（默认） |
| `torrent_count` | 选种子数最少的节点 |
| `free_space` | 选剩余空间最大的节点 |
| `all` | 综合评分（上传 40% + 种子数 40% + 空间 20%） |

---

## 管理面板

访问 `http://host:5000/admin`，需在请求头中携带 `X-Admin-Secret` 或 URL 参数 `?secret=` 进行鉴权（`ADMIN_SECRET` 为空时跳过）。

| 接口 | 方法 | 功能 |
|------|------|------|
| `/admin/status` | GET | 查看运行状态 |
| `/admin/servers` | GET/POST/DELETE | 节点管理 |
| `/admin/probe` | POST | 手动探测所有节点 |
| `/admin/reload` | POST | 重载配置 |
| `/admin/env` | GET/POST | 查看/修改 `.env` |
| `/admin/logs` | GET | 查看最近日志 |
| `/health` | GET | 健康检查 |

---

## 日志

日志写入 `torrent_webhook.log`，5 MB 自动轮转，保留 3 份备份，同时输出到控制台。