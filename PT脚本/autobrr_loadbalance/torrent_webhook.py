import os
import sys
import time
import logging
import threading
from datetime import datetime
from typing import List, Optional
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler

from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv
import requests

load_dotenv()

# ── 日志：5 MB 轮转，保留 3 份备份 ─────────────────────────────────────────
_log_handler = RotatingFileHandler(
    'torrent_webhook.log', maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8'
)
_log_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logging.basicConfig(level=logging.INFO, handlers=[_log_handler, logging.StreamHandler()])
logger = logging.getLogger(__name__)

# ── 全局常量 ────────────────────────────────────────────────────────────────
START_TIME = datetime.now()

# ── 共享 Vertex Cookie 模块 ────────────────────────────────────────────────
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
try:
    from vertex_cookie import VertexCookieManager
    _VCM_AVAILABLE = True
except ImportError:
    _VCM_AVAILABLE = False
    logger.warning("[vertex_cookie] 模块未找到，Cookie 将不会自动刷新")

app = Flask(__name__)

# ── 选择策略常量 ────────────────────────────────────────────────────────────────
STRATEGY_UPLOAD_SPEED  = 'upload_speed'
STRATEGY_TORRENT_COUNT = 'torrent_count'
STRATEGY_FREE_SPACE    = 'free_space'
STRATEGY_ALL           = 'all'
VALID_STRATEGIES = {STRATEGY_UPLOAD_SPEED, STRATEGY_TORRENT_COUNT, STRATEGY_FREE_SPACE, STRATEGY_ALL}


# ── 数据类 ──────────────────────────────────────────────────────────────────────
@dataclass
class QBServer:
    url: str
    username: str
    password: str
    proxy_id:      str  = ''    # VT 模式下的原始 proxy_id（如 d85456e1）
    upload_speed:  Optional[int] = None
    torrent_count: Optional[int] = None
    free_space:    Optional[int] = None
    available:     bool = False
    auth_error:    bool = False

    def __repr__(self):
        return (f"QBServer(url={self.url}, "
                f"proxy_id={self.proxy_id}, "
                f"torrents={self.torrent_count}, "
                f"upload={self.upload_speed}, "
                f"free={self.free_space})")


@dataclass
class VTProxy:
    proxy_id: str

    def get_proxy_url(self, vt_url: str) -> str:
        return f"{vt_url.rstrip('/')}/proxy/client/{self.proxy_id}"


# ── Telegram 通知 ───────────────────────────────────────────────────────────────
class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id   = chat_id
        self.enabled   = bool(bot_token and chat_id)

    def send(self, message: str):
        if not self.enabled:
            return
        try:
            url  = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            resp = requests.post(url, json={"chat_id": self.chat_id,
                                            "text": message,
                                            "parse_mode": "HTML"}, timeout=10)
            if resp.status_code != 200:
                logger.warning(f"Telegram 通知失败: {resp.text}")
        except Exception as e:
            logger.error(f"Telegram 通知异常: {e}")


# ── 服务器信息采集（单次，并发） ─────────────────────────────────────────────────
def _fetch_server_info(server: QBServer, use_vt: bool, vt_cookies: str, timeout: int = 8):
    """
    并发线程内执行。连接失败直接标记 available=False 并返回，不重试。
    掉线服务器不会出现在后续的推送候选列表中。
    """
    session = requests.Session()
    try:
        if use_vt:
            session.headers.update({'Cookie': vt_cookies})
        else:
            resp = session.post(
                f"{server.url}/api/v2/auth/login",
                data={'username': server.username, 'password': server.password},
                timeout=timeout
            )
            if resp.text != 'Ok.':
                logger.warning(f"[{server.url}] 登录失败: {resp.text}")
                return

        r_transfer = session.get(f"{server.url}/api/v2/transfer/info", timeout=timeout)
        r_transfer.raise_for_status()
        if r_transfer.status_code == 200:
            d = r_transfer.json()
            server.upload_speed = d.get('up_info_speed', 0)
            server.free_space   = d.get('free_space_on_disk', 0)

        r_torrents = session.get(f"{server.url}/api/v2/torrents/info", timeout=timeout)
        r_torrents.raise_for_status()
        if r_torrents.status_code == 200:
            server.torrent_count = len(r_torrents.json())

        server.available = True
        logger.info(
            f"[{server.url}] ✓ 在线 | "
            f"上传={server.upload_speed/1024/1024:.2f}MB/s | "
            f"种子={server.torrent_count} | "
            f"空间={server.free_space/1024/1024/1024:.2f}GB"
        )

    except requests.exceptions.ConnectionError:
        logger.warning(f"[{server.url}] ✗ 连接失败（服务器已下线），已剔除")
    except requests.exceptions.Timeout:
        logger.warning(f"[{server.url}] ✗ 连接超时，已剔除")
    except Exception as e:
        if hasattr(e, 'response') and e.response is not None and e.response.status_code in (401, 403):
            server.auth_error = True
            logger.warning(f"[{server.url}] ✗ 鉴权失败 (Cookie 可能失效)")
        else:
            logger.warning(f"[{server.url}] ✗ 采集异常，已剔除: {e}")
    finally:
        session.close()


def collect_all_servers(servers: List[QBServer], use_vt: bool, vt_cookies: str, timeout: int = 8) -> List[QBServer]:
    """
    并发探测所有服务器。
    返回值：仅包含本次成功响应的服务器列表。
    掉线/超时服务器不会出现在返回值中，也不会被选为推送目标。
    """
    for s in servers:
        s.available     = False
        s.upload_speed  = None
        s.torrent_count = None
        s.free_space    = None
        s.auth_error    = False

    threads = [
        threading.Thread(target=_fetch_server_info, args=(s, use_vt, vt_cookies, timeout), daemon=True)
        for s in servers
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout + 2)

    available = [s for s in servers if s.available]
    offline   = [s for s in servers if not s.available]

    logger.info(f"探测完毕：{len(available)} 在线 / {len(offline)} 下线")
    if offline:
        logger.warning(f"下线服务器（本次跳过）: {[s.url for s in offline]}")

    return available


# ── 服务器选择 ──────────────────────────────────────────────────────────────────
def select_best_server(available: List[QBServer], strategy: str = STRATEGY_UPLOAD_SPEED) -> Optional[QBServer]:
    if not available:
        return None

    if strategy == STRATEGY_UPLOAD_SPEED:
        best = min(available, key=lambda s: s.upload_speed or 0)
        logger.info(f"策略[上传速度最低] → {best.url} ({(best.upload_speed or 0)/1024/1024:.2f} MB/s)")
    elif strategy == STRATEGY_TORRENT_COUNT:
        best = min(available, key=lambda s: s.torrent_count or 0)
        logger.info(f"策略[种子数最少] → {best.url} ({best.torrent_count})")
    elif strategy == STRATEGY_FREE_SPACE:
        best = max(available, key=lambda s: s.free_space or 0)
        logger.info(f"策略[剩余空间最大] → {best.url} ({(best.free_space or 0)/1024/1024/1024:.2f} GB)")
    elif strategy == STRATEGY_ALL:
        max_upload = max((s.upload_speed  or 0 for s in available), default=1) or 1
        max_count  = max((s.torrent_count or 0 for s in available), default=1) or 1
        max_space  = max((s.free_space    or 0 for s in available), default=1) or 1
        def score(s):
            return ((s.upload_speed or 0)/max_upload * 0.4 +
                    (s.torrent_count or 0)/max_count * 0.4 +
                    (1 - (s.free_space or 0)/max_space) * 0.2)
        best = min(available, key=score)
        logger.info(f"策略[综合评分] → {best.url} (score={score(best):.4f})")
    else:
        logger.warning(f"未知策略 '{strategy}'，使用第一台可用服务器")
        best = available[0]

    return best


# ── 推送种子 ────────────────────────────────────────────────────────────────────
def _add_torrent_to_server(server: QBServer, use_vt: bool, vt_cookies: str,
                           download_url: str, category: str, torrent_name: str,
                           timeout: int = 30) -> bool:
    session = requests.Session()
    try:
        if use_vt:
            session.headers.update({'Cookie': vt_cookies})
        else:
            resp = session.post(
                f"{server.url}/api/v2/auth/login",
                data={'username': server.username, 'password': server.password},
                timeout=8
            )
            if resp.text != 'Ok.':
                logger.error(f"[{server.url}] 推送前登录失败")
                return False

        resp = session.post(
            f"{server.url}/api/v2/torrents/add",
            data={'urls': download_url, 'category': category, 'paused': 'false'},
            timeout=timeout
        )
        if resp.status_code == 200 and resp.text == 'Ok.':
            logger.info(f"✓ 推送成功 → {server.url} | {torrent_name}")
            return True
        else:
            logger.error(f"✗ 推送失败 → {server.url} | 响应: {resp.text}")
            return False

    except requests.exceptions.ConnectionError:
        logger.error(f"✗ 推送时连接断开 → {server.url}（采集后下线？）")
        return False
    except requests.exceptions.Timeout:
        logger.error(f"✗ 推送超时 → {server.url}")
        return False
    except Exception as e:
        logger.error(f"✗ 推送异常 → {server.url}: {e}")
        return False
    finally:
        session.close()


# ── .env 工具 ──────────────────────────────────────────────────────────────────
def _env_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')

def _update_env_key(key: str, value: str):
    path = _env_path()
    if not os.path.exists(path):
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f"{key}={value}\n")
        return
    with open(path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    found, new_lines = False, []
    for line in lines:
        if line.startswith(f"{key}=") or line.startswith(f"{key} ="):
            new_lines.append(f"{key}={value}\n")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}\n")
    with open(path, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)

def _read_env_file() -> str:
    path = _env_path()
    if not os.path.exists(path):
        return ''
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()

def _write_env_file(content: str):
    with open(_env_path(), 'w', encoding='utf-8') as f:
        f.write(content)


# ── 分发器 ──────────────────────────────────────────────────────────────────────
class TorrentDistributor:
    def __init__(self):
        self.qb_servers:  List[QBServer] = []
        self.use_vt_mode: bool = False
        self.vt_url:      str  = ''
        self.vt_cookies:  str  = ''
        self.telegram:    Optional[TelegramNotifier] = None
        self._vcm:        Optional[object] = None
        self._lock        = threading.Lock()
        self._load_config()

        # 启动后台轮转探测线程
        self._stop_event = threading.Event()
        self._poll_thread = threading.Thread(target=self._background_worker, daemon=True)
        self._poll_thread.start()

    def _load_config(self):
        self.telegram = TelegramNotifier(
            os.getenv('TG_BOT_TOKEN', ''), os.getenv('TG_CHAT_ID', '')
        )
        self.vt_url      = os.getenv('VTURL', '')
        self.vt_cookies  = os.getenv('VTCOOKIES', '')
        self.use_vt_mode = bool(self.vt_url)

        if _VCM_AVAILABLE and self.vt_url:
            username = os.getenv('VT_USERNAME', 'admin')
            plain    = os.getenv('VT_PASSWORD', '')
            hashed   = os.getenv('VT_PASSWORD_MD5', '')
            if username and (plain or hashed):
                kwargs = dict(
                    login_url      = self.vt_url,
                    username       = username,
                    check_interval = int(os.getenv('VT_COOKIE_CHECK_INTERVAL', '300')),
                )
                if plain:
                    kwargs['password'] = plain
                else:
                    kwargs['password'] = hashed
                    kwargs['password_is_hashed'] = True
                self._vcm = VertexCookieManager(**kwargs)
                self.vt_cookies = self._vcm.get_valid_cookie() or self.vt_cookies
                logger.info("[VTCookie] 自动刷新管理器已启动")

        if not self.vt_cookies:
            self.use_vt_mode = False

        if self.use_vt_mode:
            logger.info("=== Vertex 代理模式 ===")
            self._load_vt_proxies()
        else:
            logger.info("=== 标准 qBittorrent 模式 ===")
            self._load_qb_servers()

    def reload_config(self):
        """热重载：重新读 .env，重建服务器列表，无需重启进程"""
        with self._lock:
            load_dotenv(override=True)
            self.qb_servers.clear()
            self._vcm = None
            self._load_config()
            logger.info(f"[热重载] 完成，当前 {len(self.qb_servers)} 台服务器")

    def _load_vt_proxies(self):
        raw = os.getenv('VT_PROXIES', '')
        if not raw:
            logger.error("VT 模式启用但 VT_PROXIES 未配置")
            return
        for pid in (p.strip() for p in raw.split(',') if p.strip()):
            url = VTProxy(pid).get_proxy_url(self.vt_url)
            self.qb_servers.append(QBServer(url=url, username='', password='', proxy_id=pid))
        logger.info(f"已加载 {len(self.qb_servers)} 个 VT 代理")

    def _load_qb_servers(self):
        raw = os.getenv('QB_SERVERS', '')
        if not raw:
            logger.error("QB_SERVERS 未配置")
            return
        u = os.getenv('QB_USERNAME', '')
        p = os.getenv('QB_PASSWORD', '')
        for url in (x.strip() for x in raw.split(',') if x.strip()):
            self.qb_servers.append(QBServer(url=url, username=u, password=p))
        logger.info(f"已加载 {len(self.qb_servers)} 台 qBittorrent 服务器")

    def add_server(self, identifier: str, username: str = '', password: str = '') -> bool:
        """
        VT 模式：identifier = proxy_id（如 d85456e1），自动拼接完整 URL。
        QB 模式：identifier = 完整 URL。
        """
        with self._lock:
            identifier = identifier.strip().rstrip('/')
            if self.use_vt_mode:
                # 去重：检查 proxy_id
                if any(s.proxy_id == identifier for s in self.qb_servers):
                    return False
                url = VTProxy(identifier).get_proxy_url(self.vt_url)
                self.qb_servers.append(QBServer(url=url, username='', password='', proxy_id=identifier))
                logger.info(f"[动态添加 VT] proxy_id={identifier} → {url}")
            else:
                if any(s.url == identifier for s in self.qb_servers):
                    return False
                self.qb_servers.append(QBServer(url=identifier, username=username, password=password))
                logger.info(f"[动态添加 QB] {identifier}")
            self._persist_servers()
            return True

    def remove_server(self, identifier: str) -> bool:
        """
        VT 模式：identifier = proxy_id。
        QB 模式：identifier = 完整 URL。
        """
        with self._lock:
            identifier = identifier.strip().rstrip('/')
            before = len(self.qb_servers)
            if self.use_vt_mode:
                self.qb_servers = [s for s in self.qb_servers if s.proxy_id != identifier]
            else:
                self.qb_servers = [s for s in self.qb_servers if s.url != identifier]
            if len(self.qb_servers) == before:
                return False
            self._persist_servers()
            logger.info(f"[动态删除] {identifier}")
            return True

    def _persist_servers(self):
        if self.use_vt_mode:
            _update_env_key('VT_PROXIES', ','.join(s.proxy_id for s in self.qb_servers if s.proxy_id))
        else:
            _update_env_key('QB_SERVERS', ','.join(s.url for s in self.qb_servers))

    def get_servers_status(self) -> list:
        result = []
        for s in self.qb_servers:
            entry = {
                'url':          s.url,
                'available':    s.available,
                'upload_speed': s.upload_speed,
                'torrent_count':s.torrent_count,
                'free_space':   s.free_space,
            }
            if self.use_vt_mode:
                entry['proxy_id'] = s.proxy_id   # 前端用这个字段做删除 key
            result.append(entry)
        return result

    def _refresh_cookie(self) -> str:
        if self._vcm:
            c = self._vcm.force_refresh()
            if c:
                self.vt_cookies = c
        return self.vt_cookies

    def _get_cookie(self) -> str:
        if self._vcm:
            c = self._vcm.get_valid_cookie()
            if c:
                self.vt_cookies = c
        return self.vt_cookies

    def _background_worker(self):
        """后台循环：每隔一段时间刷新 Cookie 并探测服务器状态"""
        logger.info("[Background] 后台监控线程已启动")
        
        # 初始等待一下，确保系统启动完成
        time.sleep(2)
        
        while not self._stop_event.is_set():
            try:
                # 1. 如果是 VT 模式，获取/刷新 Cookie
                current_cookies = ""
                if self.use_vt_mode:
                    current_cookies = self._get_cookie()
                    if not current_cookies:
                        logger.warning("[Background] 无法获取有效 VT Cookie，跳过本轮状态探测")
                
                # 2. 探测所有服务器状态
                with self._lock:
                    servers_snapshot = list(self.qb_servers)
                
                if servers_snapshot:
                    logger.debug(f"[Background] 开始探测 {len(servers_snapshot)} 台服务器状态...")
                    # 探测间隔可以比推送间隔稍长，比如 60 秒
                    available = collect_all_servers(servers_snapshot, self.use_vt_mode, current_cookies, timeout=10)
                    
                    # 3. 特殊逻辑：如果 VT 模式下有服务器返回 401/403，尝试强制刷新 Cookie
                    if self.use_vt_mode and any(s.auth_error for s in servers_snapshot):
                        logger.warning("[Background] 检测到鉴权错误，强制刷新 Cookie...")
                        self._refresh_cookie()
                
            except Exception as e:
                logger.error(f"[Background] 后台任务异常: {e}")
            
            # 每 60 秒执行一次探测
            # 如果是 VT 模式，VertexCookieManager 内部也有自己的 check_interval (默认 300s)
            time.sleep(60)

    def distribute(self, release_name: str, indexer: str, download_url: str) -> bool:
        logger.info("=" * 60)
        logger.info(f"新推送 | {release_name} | {indexer}")

        strategy      = os.getenv('SELECT_STRATEGY', STRATEGY_UPLOAD_SPEED)
        max_retries   = min(int(os.getenv('MAX_RETRIES', '3')), 3)
        retry_delay   = int(os.getenv('RETRY_DELAY', '5'))
        fetch_timeout = int(os.getenv('FETCH_TIMEOUT', '8'))

        if strategy not in VALID_STRATEGIES:
            strategy = STRATEGY_UPLOAD_SPEED

        current_cookies = self._get_cookie()
        if self.use_vt_mode and not current_cookies:
            logger.error("VT Cookie 为空，放弃推送")
            return False

        # ── 每次推送前重新并发探测，只对在线服务器推送 ──────────────────────
        with self._lock:
            servers_snapshot = list(self.qb_servers)

        logger.info(f"▶ 探测 {len(servers_snapshot)} 台服务器...")
        available = collect_all_servers(servers_snapshot, self.use_vt_mode, current_cookies, timeout=fetch_timeout)

        if not available:
            msg = (f"❌ <b>无可用服务器</b>\n\n"
                   f"📦 <b>名称:</b> {release_name}\n"
                   f"🏷 <b>分类:</b> {indexer}\n"
                   f"❗️ 全部服务器无响应，放弃推送")
            logger.error("所有服务器不可用")
            self.telegram.send(msg)
            return False

        best = select_best_server(available, strategy)
        if not best:
            return False

        # ── 向选定的在线服务器推送，失败可重试但不换服务器 ──────────────────
        for attempt in range(1, max_retries + 1):
            logger.info(f"▶ 推送第 {attempt}/{max_retries} 次 → {best.url}")
            use_cookies = self.vt_cookies
            ok = _add_torrent_to_server(best, self.use_vt_mode, use_cookies, download_url, indexer, release_name)

            # 仅第一次失败时尝试刷新 Cookie
            if not ok and attempt == 1 and self._vcm:
                logger.warning("[VTCookie] 尝试刷新 Cookie 后重推...")
                refreshed = self._refresh_cookie()
                if refreshed and refreshed != use_cookies:
                    ok = _add_torrent_to_server(best, self.use_vt_mode, refreshed, download_url, indexer, release_name)

            if ok:
                self.telegram.send(
                    f"✅ <b>种子添加成功</b>\n\n"
                    f"📦 <b>名称:</b> {release_name}\n"
                    f"🏷 <b>分类:</b> {indexer}\n"
                    f"🖥 <b>服务器:</b> {best.url}\n"
                    f"⬆️ <b>上传速度:</b> {(best.upload_speed or 0)/1024/1024:.2f} MB/s\n"
                    f"📊 <b>种子数:</b> {best.torrent_count}\n"
                    f"💾 <b>剩余空间:</b> {(best.free_space or 0)/1024/1024/1024:.2f} GB\n"
                    f"🔄 <b>尝试次数:</b> {attempt}/{max_retries}\n"
                    f"📐 <b>策略:</b> {strategy}"
                )
                return True

            if attempt < max_retries:
                time.sleep(retry_delay)

        self.telegram.send(
            f"❌ <b>种子添加失败</b>\n\n"
            f"📦 <b>名称:</b> {release_name}\n"
            f"🏷 <b>分类:</b> {indexer}\n"
            f"🖥 <b>目标服务器:</b> {best.url}\n"
            f"❗️ 服务器在线但推送 {max_retries} 次均失败"
        )
        return False


# ── 全局实例 ────────────────────────────────────────────────────────────────────
distributor = TorrentDistributor()


# ── 鉴权 ────────────────────────────────────────────────────────────────────────
def _admin_auth():
    secret = os.getenv('ADMIN_SECRET', '')
    if not secret:
        return True
    provided = request.headers.get('X-Admin-Secret') or request.args.get('secret', '')
    return provided == secret


# ── Webhook ─────────────────────────────────────────────────────────────────────
@app.route('/<path:webhook_path>', methods=['POST'])
def webhook_handler(webhook_path):
    expected = os.getenv('WEBHOOK_PATH', 'webhook/secure-a812c2e1-4b1d-9813-ab113-ef489')
    if webhook_path != expected:
        return jsonify({'status': 'error', 'message': 'Invalid webhook path'}), 404
    try:
        data = request.get_json()
        if not data:
            return jsonify({'status': 'error', 'message': 'No data'}), 400
        release_name = data.get('release_name', '')
        indexer      = data.get('indexer', '')
        download_url = data.get('download_url', '')
        if not all([release_name, indexer, download_url]):
            return jsonify({'status': 'error', 'message': 'Incomplete data'}), 400
        success = distributor.distribute(release_name, indexer, download_url)
        return jsonify({'status': 'success' if success else 'error'}), 200 if success else 500
    except Exception as e:
        logger.error(f"Webhook 异常: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        'status':    'healthy',
        'mode':      'VT Proxy' if distributor.use_vt_mode else 'Standard qBittorrent',
        'servers':   len(distributor.qb_servers),
        'strategy':  os.getenv('SELECT_STRATEGY', STRATEGY_UPLOAD_SPEED),
        'timestamp': datetime.now().isoformat()
    })


# ── 管理 API ────────────────────────────────────────────────────────────────────
@app.route('/admin', methods=['GET'])
def admin_ui():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'admin.html')


@app.route('/admin/status', methods=['GET'])
def admin_status():
    if not _admin_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify({
        'mode':     'vt' if distributor.use_vt_mode else 'qb',
        'strategy': os.getenv('SELECT_STRATEGY', STRATEGY_UPLOAD_SPEED),
        'servers':  len(distributor.qb_servers),
        'telegram': distributor.telegram.enabled if distributor.telegram else False,
        'vt_url':   distributor.vt_url,
        'uptime':   str(datetime.now() - START_TIME).split('.')[0], # 格式示例: 1 day, 0:05:22
        'start_at': START_TIME.isoformat(),
    })


@app.route('/admin/servers', methods=['GET'])
def admin_list_servers():
    if not _admin_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify({'mode': 'vt' if distributor.use_vt_mode else 'qb', 'servers': distributor.get_servers_status()})


@app.route('/admin/servers', methods=['POST'])
def admin_add_server():
    if not _admin_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json() or {}
    # VT模式传 proxy_id，QB模式传 url（统一用 identifier 字段，兼容旧 url 字段）
    identifier = (data.get('identifier') or data.get('url') or '').strip()
    if not identifier:
        return jsonify({'error': 'identifier (proxy_id or url) required'}), 400
    ok = distributor.add_server(identifier, data.get('username', ''), data.get('password', ''))
    return jsonify({'status': 'added' if ok else 'exists', 'identifier': identifier}), 200 if ok else 409


@app.route('/admin/servers', methods=['DELETE'])
def admin_remove_server():
    if not _admin_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json() or {}
    identifier = (data.get('identifier') or data.get('url') or '').strip()
    if not identifier:
        return jsonify({'error': 'identifier (proxy_id or url) required'}), 400
    ok = distributor.remove_server(identifier)
    return jsonify({'status': 'removed' if ok else 'not found', 'identifier': identifier}), 200 if ok else 404


@app.route('/admin/probe', methods=['POST'])
def admin_probe():
    if not _admin_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    timeout = int(request.args.get('timeout', 8))
    with distributor._lock:
        snapshot = list(distributor.qb_servers)
    available = collect_all_servers(snapshot, distributor.use_vt_mode, distributor.vt_cookies, timeout=timeout)
    return jsonify({
        'total':   len(snapshot),
        'online':  len(available),
        'offline': len(snapshot) - len(available),
        'servers': distributor.get_servers_status(),
    })


@app.route('/admin/reload', methods=['POST'])
def admin_reload():
    if not _admin_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    distributor.reload_config()
    return jsonify({'status': 'reloaded', 'servers': len(distributor.qb_servers)})


@app.route('/admin/env', methods=['GET'])
def admin_get_env():
    if not _admin_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify({'content': _read_env_file()})


@app.route('/admin/env', methods=['POST'])
def admin_save_env():
    if not _admin_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    data    = request.get_json() or {}
    content = data.get('content', '')
    _write_env_file(content)
    distributor.reload_config()
    return jsonify({'status': 'saved_and_reloaded'})


@app.route('/admin/logs', methods=['GET'])
def admin_get_logs():
    if not _admin_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    n = int(request.args.get('lines', 200))
    if not os.path.exists('torrent_webhook.log'):
        return jsonify({'lines': []})
    with open('torrent_webhook.log', 'r', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()
    return jsonify({'lines': lines[-n:]})


# ── 启动 ────────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    webhook_path = os.getenv('WEBHOOK_PATH', 'webhook/secure-a812c2e1-4b1d-9813-ab113-ef489')
    port = int(os.getenv('FLASK_PORT', '5000'))
    logger.info("=" * 60)
    logger.info(f"Webhook: /{webhook_path}  端口: {port}")
    logger.info(f"模式: {'Vertex 代理' if distributor.use_vt_mode else '标准 qBittorrent'}")
    logger.info(f"服务器数: {len(distributor.qb_servers)}")
    logger.info(f"管理面板: http://0.0.0.0:{port}/admin")
    logger.info("=" * 60)
    app.run(host='0.0.0.0', port=port, debug=False)