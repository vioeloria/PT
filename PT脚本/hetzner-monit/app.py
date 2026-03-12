#!/usr/bin/env python3
"""
Hetzner Cloud Web Manager - Backend API
支持 Vertex 下载器同步 / 服务器流量监控 / 自动重建 / 定时删建机器
"""

import requests
import json
import time
import logging
import os
import re
import sys
import threading
import secrets
from collections import Counter
from datetime import datetime, time as dt_time
from typing import Optional, List, Dict, Tuple
from functools import wraps
from logging.handlers import RotatingFileHandler
from flask import Flask, jsonify, request, session, send_from_directory

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

_rotating_handler = RotatingFileHandler(
    'hetzner_web.log', maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8'
)
_rotating_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logging.basicConfig(level=logging.INFO, handlers=[_rotating_handler, logging.StreamHandler()])
logger = logging.getLogger(__name__)

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
if _MODULE_DIR not in sys.path:
    sys.path.insert(0, _MODULE_DIR)

try:
    from vertex_cookie import VertexCookieManager
    _VCM_AVAILABLE = True
except ImportError:
    _VCM_AVAILABLE = False
    logging.getLogger(__name__).warning("[vertex_cookie] 模块未找到，Cookie 将不会自动刷新")

app = Flask(__name__, static_folder='static')
app.secret_key = os.getenv('FLASK_SECRET_KEY', secrets.token_hex(32))

# ─── 服务器型号完整映射表 ──────────────────────────────────────────────────────
SERVER_TYPE_CATALOG: Dict[str, Dict] = {
    "cx22":  {"cores": 2,  "memory": 4,   "disk": 40,  "traffic": "20TB",           "series": "CX",  "arch": "x86"},
    "cx32":  {"cores": 4,  "memory": 8,   "disk": 80,  "traffic": "20TB",           "series": "CX",  "arch": "x86"},
    "cx33":  {"cores": 4,  "memory": 8,   "disk": 80,  "traffic": "20TB",           "series": "CX",  "arch": "x86"},
    "cx43":  {"cores": 8,  "memory": 16,  "disk": 160, "traffic": "20TB",           "series": "CX",  "arch": "x86"},
    "cx53":  {"cores": 16, "memory": 32,  "disk": 320, "traffic": "20TB",           "series": "CX",  "arch": "x86"},
    "cpx11": {"cores": 2,  "memory": 2,   "disk": 40,  "traffic": "20TB",           "series": "CPX", "arch": "x86"},
    "cpx21": {"cores": 3,  "memory": 4,   "disk": 80,  "traffic": "20TB",           "series": "CPX", "arch": "x86"},
    "cpx22": {"cores": 2,  "memory": 4,   "disk": 80,  "traffic": "20TB/0.5TB(SIN)","series": "CPX", "arch": "x86"},
    "cpx31": {"cores": 4,  "memory": 8,   "disk": 160, "traffic": "20TB",           "series": "CPX", "arch": "x86"},
    "cpx32": {"cores": 4,  "memory": 8,   "disk": 160, "traffic": "20TB/0.5TB(SIN)","series": "CPX", "arch": "x86"},
    "cpx41": {"cores": 8,  "memory": 16,  "disk": 240, "traffic": "20TB",           "series": "CPX", "arch": "x86"},
    "cpx42": {"cores": 8,  "memory": 16,  "disk": 320, "traffic": "20TB/0.5TB(SIN)","series": "CPX", "arch": "x86"},
    "cpx51": {"cores": 16, "memory": 32,  "disk": 360, "traffic": "20TB",           "series": "CPX", "arch": "x86"},
    "cax11": {"cores": 2,  "memory": 4,   "disk": 40,  "traffic": "20TB",           "series": "CAX", "arch": "arm64"},
    "cax21": {"cores": 4,  "memory": 8,   "disk": 80,  "traffic": "20TB",           "series": "CAX", "arch": "arm64"},
    "cax31": {"cores": 8,  "memory": 16,  "disk": 160, "traffic": "20TB",           "series": "CAX", "arch": "arm64"},
    "cax41": {"cores": 16, "memory": 32,  "disk": 320, "traffic": "20TB",           "series": "CAX", "arch": "arm64"},
}

# ─── Config ────────────────────────────────────────────────────────────────────
CONFIG_FILE = 'config.json'
DEFAULT_CONFIG = {
    "hetzner_api_key": "",
    "traffic_threshold": 0.8,
    "check_interval": 1200,
    "max_servers": 3,
    "initial_snapshot_id": "",
    "ssh_keys": [],
    "server_types": ["cx43", "cpx32", "cx43", "cpx42", "cpx22"],
    "default_location": "nbg1",
    "enable_time_window": False,
    "work_start_hour": 8,
    "work_end_hour": 23,
    "work_end_minute": 30,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "web_password": "admin123",
    "auto_rebuild_enabled": True,
    "vertex_api_url": "",
    "vertex_cookies": "",
    "vertex_sync_enabled": True,
    "vertex_downloader_keyword": "Hetzner",
    "vertex_username": "",
    "vertex_password": "",
    "vertex_password_md5": "",
    "vertex_cookie_check_interval": 300,
    # ── 定时删建配置 ──
    "scheduled_tasks_enabled": False,       # 总开关
    "schedule_timezone": "Asia/Shanghai",   # 时区（IANA 名称）
    "schedule_delete_enabled": False,       # 定时删除开关
    "schedule_delete_hour": 23,             # 删除时刻（小时，本地时间）
    "schedule_delete_minute": 0,            # 删除时刻（分钟）
    "schedule_create_enabled": False,       # 定时创建开关
    "schedule_create_hour": 8,              # 创建时刻（小时，本地时间）
    "schedule_create_minute": 0,            # 创建时刻（分钟）
    "schedule_create_count": 3,             # 定时创建台数
    "schedule_server_name_prefix": "hetzner-auto",  # 创建时名称前缀
}


def load_config() -> Dict:
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
                for k, v in DEFAULT_CONFIG.items():
                    if k not in cfg:
                        cfg[k] = v
                return cfg
    except Exception as e:
        logger.error(f"Load config error: {e}")
    return DEFAULT_CONFIG.copy()


def save_config(cfg: Dict):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Save config error: {e}")


# ─── Monitor State ─────────────────────────────────────────────────────────────
monitor_state = {
    "running": False,
    "last_check": None,
    "next_check": None,
    "servers_cache": [],
    "logs": [],
    "stop_event": threading.Event(),
    # 定时任务状态
    "scheduler_running": False,
    "scheduler_stop_event": threading.Event(),
    "last_scheduled_delete": None,
    "last_scheduled_create": None,
    "next_scheduled_delete": None,
    "next_scheduled_create": None,
}


def add_log(msg: str, level: str = "info"):
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg, "level": level}
    monitor_state["logs"].insert(0, entry)
    if len(monitor_state["logs"]) > 200:
        monitor_state["logs"] = monitor_state["logs"][:200]
    getattr(logger, "warning" if level == "warn" else level, logger.info)(msg)


# ─── Hetzner API ───────────────────────────────────────────────────────────────
class HetznerAPI:
    BASE = "https://api.hetzner.cloud/v1"

    def __init__(self, api_key: str):
        self.headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def _get(self, path: str, params: dict = None):
        r = requests.get(f"{self.BASE}{path}", headers=self.headers, params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    def get_servers(self) -> List[Dict]:
        try:
            return self._get("/servers").get("servers", [])
        except Exception as e:
            add_log(f"获取服务器列表失败: {e}", "error"); return []

    def get_server(self, server_id: int) -> Optional[Dict]:
        try:
            r = requests.get(f"{self.BASE}/servers/{server_id}", headers=self.headers, timeout=15)
            return None if r.status_code == 404 else r.json().get("server")
        except Exception as e:
            add_log(f"获取服务器 {server_id} 失败: {e}", "error"); return None

    def get_images(self, image_type: str = "snapshot") -> List[Dict]:
        try:
            return self._get("/images", {"type": image_type, "include_deprecated": "false"}).get("images", [])
        except Exception as e:
            add_log(f"获取镜像失败: {e}", "error"); return []

    def get_ssh_keys(self) -> List[Dict]:
        try:
            return self._get("/ssh_keys").get("ssh_keys", [])
        except Exception as e:
            add_log(f"获取SSH密钥失败: {e}", "error"); return []

    @staticmethod
    def sanitize_name(name: str) -> str:
        import re as _re
        n = name.lower()
        n = _re.sub(r'[^a-z0-9-]', '-', n)
        n = _re.sub(r'-{2,}', '-', n)
        n = n.strip('-')
        n = n[:63]
        n = n.rstrip('-')
        if not n:
            n = f"server-{int(time.time())}"
        return n

    def create_server(self, name: str, server_type: str, image_id: int,
                      ssh_keys: List, location: str = "nbg1") -> Optional[Dict]:
        safe_name = self.sanitize_name(name)
        if safe_name != name:
            add_log(f"  名称规范化: '{name}' → '{safe_name}'")
        try:
            r = requests.post(f"{self.BASE}/servers", headers=self.headers, timeout=30, json={
                "name": safe_name, "server_type": server_type, "image": int(image_id),
                "location": location, "ssh_keys": ssh_keys,
                "public_net": {"enable_ipv4": True, "enable_ipv6": True},
                "start_after_create": True
            })
            if r.status_code == 201:
                d = r.json()
                return {
                    "id": d["server"]["id"], "name": d["server"]["name"],
                    "ip": d["server"]["public_net"]["ipv4"]["ip"],
                    "server_type": d["server"]["server_type"]["name"],
                    "root_password": d.get("root_password")
                }
            err_body = r.json()
            err_code = err_body.get("error", {}).get("code", "")
            err_msg  = err_body.get("error", {}).get("message", r.text)
            add_log(f"  创建失败 [{server_type}] code={err_code}: {err_msg}", "error")
            if err_code in ("uniqueness_error", "invalid_input") and "name" in err_msg.lower():
                return {"_name_conflict": True}
            return None
        except Exception as e:
            add_log(f"  创建异常: {e}", "error"); return None

    def create_server_with_fallback(self, name: str, server_types: List[str],
                                    image_id: int, ssh_keys: List,
                                    location: str = "nbg1") -> Optional[Dict]:
        base_name = self.sanitize_name(name)
        fallback_name = self.sanitize_name(f"{base_name}-{int(time.time()) % 100000}")

        for st in server_types:
            add_log(f"  → 尝试型号 [{st}] name={base_name} ...")
            result = self.create_server(base_name, st, image_id, ssh_keys, location)

            if result and not result.get("_name_conflict"):
                add_log(f"  ✅ [{st}] 创建成功: {result['ip']}")
                return result

            if result and result.get("_name_conflict"):
                add_log(f"  ⚠ 名称冲突，改用备用名称 [{fallback_name}] 重试...", "warn")
                result2 = self.create_server(fallback_name, st, image_id, ssh_keys, location)
                if result2 and not result2.get("_name_conflict"):
                    add_log(f"  ✅ [{st}] 备用名创建成功: {result2['ip']}")
                    return result2
                add_log(f"  ✗ [{st}] 备用名仍失败，尝试下一型号...", "warn")
            else:
                add_log(f"  ✗ [{st}] 无货或其他错误，尝试下一个...", "warn")

        add_log("❌ 所有型号均失败", "error")
        return None

    def delete_server(self, server_id: int) -> bool:
        try:
            r = requests.delete(f"{self.BASE}/servers/{server_id}",
                                headers=self.headers, timeout=15)
            if r.status_code == 404:
                add_log(f"  服务器 {server_id} 已不存在（视为删除成功）")
                return True
            r.raise_for_status()
            for i in range(30):
                time.sleep(3)
                chk = requests.get(f"{self.BASE}/servers/{server_id}",
                                   headers=self.headers, timeout=10)
                if chk.status_code == 404:
                    add_log(f"  服务器 {server_id} 已确认删除 ({(i+1)*3}s)")
                    return True
            add_log(f"  服务器 {server_id} 等待删除超时", "warn")
            return False
        except Exception as e:
            add_log(f"  删除服务器 {server_id} 失败: {e}", "error"); return False


def get_hetzner() -> Optional[HetznerAPI]:
    cfg = load_config()
    return HetznerAPI(cfg["hetzner_api_key"]) if cfg.get("hetzner_api_key") else None


# ─── 全局 Cookie 管理器单例 ────────────────────────────────────────────────────
_vcm_instance: Optional["VertexCookieManager"] = None
_vcm_lock = threading.Lock()


def _build_vcm() -> Optional["VertexCookieManager"]:
    global _vcm_instance
    if not _VCM_AVAILABLE:
        return None
    with _vcm_lock:
        if _vcm_instance is None:
            cfg   = load_config()
            url   = cfg.get("vertex_api_url", "")
            user  = cfg.get("vertex_username", "")
            plain  = cfg.get("vertex_password", "")
            hashed = cfg.get("vertex_password_md5", "")
            if url and user and (plain or hashed):
                if plain:
                    _vcm_instance = VertexCookieManager(
                        login_url      = url,
                        username       = user,
                        password       = plain,
                        check_interval = int(cfg.get("vertex_cookie_check_interval", 300)),
                    )
                else:
                    _vcm_instance = VertexCookieManager(
                        login_url          = url,
                        username           = user,
                        password           = hashed,
                        password_is_hashed = True,
                        check_interval     = int(cfg.get("vertex_cookie_check_interval", 300)),
                    )
        return _vcm_instance


# ─── Vertex Downloader API ────────────────────────────────────────────────────────────
class VertexAPI:
    def __init__(self, base_url: str, cookies: str, keyword: str = "Hetzner",
                 cookie_manager=None):
        self.base_url       = base_url.rstrip('/')
        self.keyword        = keyword
        self.cookie_manager = cookie_manager
        self._cookies       = cookies
        self.headers        = {'Content-Type': 'application/json', 'Cookie': cookies}

    def _refresh_cookie(self) -> bool:
        if not self.cookie_manager:
            return False
        new_cookie = self.cookie_manager.get_valid_cookie(force_refresh=True)
        if new_cookie and new_cookie != self._cookies:
            self._cookies = new_cookie
            self.headers['Cookie'] = new_cookie
            cfg = load_config()
            cfg['vertex_cookies'] = new_cookie
            save_config(cfg)
            add_log(f"[VTCookie] Cookie 已自动刷新并保存")
            return True
        return False

    def _get(self, path: str, retry: bool = True):
        r = requests.get(f"{self.base_url}{path}", headers=self.headers, timeout=10)
        if r.status_code in (401, 403) and retry and self._refresh_cookie():
            add_log(f"Vertex: GET {path} 收到 {r.status_code}，尝试刷新 Cookie 后重试", "warn")
            r = requests.get(f"{self.base_url}{path}", headers=self.headers, timeout=10)
        r.raise_for_status()
        return r

    def _post(self, path: str, json_data=None, retry: bool = True):
        r = requests.post(f"{self.base_url}{path}", headers=self.headers,
                          json=json_data, timeout=10)
        if r.status_code in (401, 403) and retry and self._refresh_cookie():
            add_log(f"Vertex: POST {path} 收到 {r.status_code}，尝试刷新 Cookie 后重试", "warn")
            r = requests.post(f"{self.base_url}{path}", headers=self.headers,
                              json=json_data, timeout=10)
        r.raise_for_status()
        return r

    def _extract_ip(self, url: str) -> Optional[str]:
        m = re.search(r'(\d{1,3}(?:\.\d{1,3}){3})', url or '')
        return m.group(1) if m else None

    def get_all_downloaders(self) -> List[Dict]:
        try:
            r = self._get("/api/downloader/list")
            data = r.json()
            items = data.get('data', data) if isinstance(data, dict) else data
            return items if isinstance(items, list) else []
        except Exception as e:
            add_log(f"Vertex: 获取下载器失败: {e}", "error"); return []

    def get_hetzner_downloaders(self) -> List[Dict]:
        return [d for d in self.get_all_downloaders()
                if self.keyword.lower() in d.get('alias', '').lower()]

    def update_downloader_ip(self, downloader: Dict, new_ip: str) -> bool:
        alias = downloader.get('alias', '?')
        old_url = downloader.get('clientUrl', '') or ''
        old_ip = self._extract_ip(old_url)

        # ── 修复：若 clientUrl 为空，直接用 ip 字段组装 URL ──
        if not old_url:
            add_log(f"Vertex: [{alias}] clientUrl 为空，跳过", "warn")
            return False

        # 若 URL 中没有旧 IP，也尝试从其他字段提取
        if not old_ip:
            add_log(f"Vertex: [{alias}] 无法从 URL 提取 IP: {old_url}，跳过", "warn")
            return False

        updated = dict(downloader)
        updated['clientUrl'] = old_url.replace(old_ip, new_ip)

        # 同时更新 url/host 等字段（兼容不同版本的 Vertex）
        for field in ('url', 'host'):
            if field in updated and updated[field]:
                updated[field] = str(updated[field]).replace(old_ip, new_ip)

        try:
            self._post("/api/downloader/modify", json_data=updated)
            add_log(f"Vertex: [{alias}] {old_ip} → {new_ip} ✓")
            return True
        except Exception as e:
            add_log(f"Vertex: 更新 [{alias}] 失败: {e}", "error")
            return False

    def sync_with_server_ips(self, server_ips: List[str]) -> Dict[str, int]:
        """
        将 Hetzner 下载器与当前服务器 IP 列表做负载均衡分配。
        每台下载器分配一个 IP，多于服务器数量时循环分配。
        """
        if not server_ips:
            add_log("Vertex: 无服务器 IP，跳过", "warn")
            return {'updated': 0, 'kept': 0, 'failed': 0}

        downloaders = self.get_hetzner_downloaders()
        if not downloaders:
            add_log(f"Vertex: 无匹配关键词 '{self.keyword}' 的下载器", "warn")
            return {'updated': 0, 'kept': 0, 'failed': 0}

        add_log(f"Vertex: 开始同步 {len(downloaders)} 个下载器，服务器IP: {server_ips}")

        # 当前各下载器已有的 IP
        current_ips: Dict[str, Optional[str]] = {}
        ip_counter = Counter()
        for dl in downloaders:
            alias = dl.get('alias', '')
            ip = self._extract_ip(dl.get('clientUrl', '') or '')
            current_ips[alias] = ip
            if ip:
                ip_counter[ip] += 1

        # 冲突 IP（同一 IP 被多个下载器使用）
        duplicate_ips = {ip for ip, cnt in ip_counter.items() if cnt > 1}
        if duplicate_ips:
            add_log(f"Vertex: 检测到冲突IP: {', '.join(duplicate_ips)}", "warn")

        # 过期 IP（当前不在服务器列表中的 IP）
        stale_ips = {ip for ip in ip_counter if ip not in server_ips}
        if stale_ips:
            add_log(f"Vertex: 检测到过期IP: {', '.join(stale_ips)}", "warn")

        # ── 重新分配策略 ──
        # 1. 先保留无冲突、无过期的已有分配
        assignment: Dict[str, str] = {}
        used_ips: List[str] = []

        for dl in downloaders:
            alias = dl.get('alias', '')
            ip = current_ips.get(alias)
            if (ip and ip in server_ips
                    and ip not in duplicate_ips
                    and ip not in stale_ips):
                assignment[alias] = ip
                used_ips.append(ip)

        # 2. 为剩余下载器分配（循环轮转所有服务器 IP）
        unassigned = [dl for dl in downloaders if dl.get('alias', '') not in assignment]

        # 构建轮转队列：优先补充未被使用的 IP
        available_pool = []
        for ip in server_ips:
            if ip not in used_ips:
                available_pool.append(ip)
        # 如果池不够，循环补全
        idx = 0
        for i, dl in enumerate(unassigned):
            alias = dl.get('alias', '')
            if available_pool:
                target_ip = available_pool.pop(0)
            else:
                # 所有 IP 都已分配，循环复用
                target_ip = server_ips[idx % len(server_ips)]
                idx += 1
            assignment[alias] = target_ip

        # ── 执行更新 ──
        updated = kept = failed = 0
        for dl in downloaders:
            alias = dl.get('alias', '')
            target = assignment.get(alias)
            if not target:
                add_log(f"Vertex: [{alias}] 无分配目标IP，跳过", "warn")
                failed += 1
                continue
            cur = current_ips.get(alias)
            if cur == target:
                add_log(f"Vertex: [{alias}] IP 无需变更 ({target})")
                kept += 1
            else:
                if self.update_downloader_ip(dl, target):
                    updated += 1
                else:
                    failed += 1

        add_log(f"Vertex: 同步完成 — 更新 {updated} / 保持 {kept} / 失败 {failed}")
        return {'updated': updated, 'kept': kept, 'failed': failed}

    def test_connection(self) -> Tuple[bool, str]:
        try:
            r = self._get("/api/downloader/list")
            if r.status_code == 200:
                data = r.json()
                items = data.get('data', data) if isinstance(data, dict) else data
                return True, f"连接成功，共 {len(items) if isinstance(items, list) else '?'} 个下载器"
            return False, f"HTTP {r.status_code}"
        except Exception as e:
            return False, str(e)


def get_vertex() -> Optional[VertexAPI]:
    cfg = load_config()
    url  = cfg.get("vertex_api_url")
    user = cfg.get("vertex_username")
    pwd  = cfg.get("vertex_password") or cfg.get("vertex_password_md5")

    if url and (cfg.get("vertex_cookies") or (user and pwd)):
        vcm = _build_vcm()
        cookies = cfg.get("vertex_cookies") or ""
        if vcm:
            fresh = vcm.get_valid_cookie()
            if fresh:
                cookies = fresh
        return VertexAPI(
            cfg["vertex_api_url"], cookies,
            cfg.get("vertex_downloader_keyword", "Hetzner"),
            cookie_manager=vcm,
        )
    return None


# ─── 通用：同步 Vertex IP（从当前服务器缓存/API 获取 IP）─────────────────────
def sync_vertex_ips(reason: str = ""):
    """同步 Vertex 下载器 IP，优先用缓存，无缓存则实时拉取"""
    cfg = load_config()
    if not cfg.get("vertex_sync_enabled"):
        return
    vertex = get_vertex()
    if not vertex:
        return

    ips = [s["ipv4"] for s in monitor_state["servers_cache"] if s.get("ipv4")]
    if not ips:
        hz = get_hetzner()
        if hz:
            ips = [(s.get("public_net", {}).get("ipv4") or {}).get("ip", "")
                   for s in hz.get_servers() if s.get("status") == "running"]
            ips = [ip for ip in ips if ip]

    if not ips:
        add_log(f"Vertex 同步跳过：无可用服务器IP{' ('+reason+')' if reason else ''}", "warn")
        return

    add_log(f"🔄 Vertex 同步 [{reason}]，IP列表: {ips}")
    vertex.sync_with_server_ips(ips)


# ─── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(msg: str):
    cfg = load_config()
    token, chat_id = cfg.get("telegram_bot_token"), cfg.get("telegram_chat_id")
    if not token or not chat_id:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML",
                            "disable_web_page_preview": True}, timeout=10)
    except Exception as e:
        add_log(f"Telegram 失败: {e}", "warn")


# ─── Server Enricher ───────────────────────────────────────────────────────────
def enrich_server(s: Dict) -> Dict:
    outgoing = int(s.get("outgoing_traffic") or 0)
    included = int(s.get("included_traffic") or 1)
    ratio = outgoing / included if included > 0 else 0
    pub = s.get("public_net", {})
    ipv4 = (pub.get("ipv4") or {}).get("ip", "")
    ipv6 = (pub.get("ipv6") or {}).get("ip", "")
    img = s.get("image") or {}
    stype_name = (s.get("server_type") or {}).get("name", "")
    return {
        "id": s["id"], "name": s["name"], "status": s.get("status", "unknown"),
        "ipv4": ipv4, "ipv6": ipv6, "server_type": stype_name,
        "server_type_info": SERVER_TYPE_CATALOG.get(stype_name, {}),
        "location": (s.get("datacenter") or {}).get("location", {}).get("name", ""),
        "datacenter": (s.get("datacenter") or {}).get("name", ""),
        "outgoing_traffic": outgoing, "included_traffic": included,
        "usage_percent": round(ratio * 100, 2), "usage_ratio": ratio,
        "created": s.get("created", ""),
        "image": {"id": img.get("id"), "name": img.get("name") or img.get("description", ""),
                  "type": img.get("type", "")}
    }


# ─── Core Monitor Logic ─────────────────────────────────────────────────────────
def do_check_and_rebuild():
    cfg = load_config()
    hz = get_hetzner()
    if not hz:
        add_log("未配置 API Key", "warn"); return

    add_log("━━━ 开始流量检查 ━━━")
    servers = hz.get_servers()
    if not servers:
        add_log("未获取到服务器", "warn"); return

    enriched = [enrich_server(s) for s in servers]
    monitor_state["servers_cache"] = enriched
    monitor_state["last_check"] = datetime.now().isoformat()

    threshold = float(cfg.get("traffic_threshold", 0.8))
    auto_rebuild = cfg.get("auto_rebuild_enabled", True)
    server_types = cfg.get("server_types", ["cx43"])
    ssh_keys = cfg.get("ssh_keys", [])
    snapshot_id = cfg.get("initial_snapshot_id", "")
    location = cfg.get("default_location", "nbg1")

    high_traffic = [s for s in enriched if s["usage_ratio"] >= threshold]
    add_log(f"📊 {len(enriched)} 台，{len(high_traffic)} 台超 {threshold*100:.0f}% 阈值")

    summary = []
    for s in enriched:
        icon = "🔴" if s["usage_ratio"] >= threshold else ("🟡" if s["usage_ratio"] >= 0.6 else "🟢")
        add_log(f"{icon} {s['name']} ({s['ipv4']}) {s['usage_percent']}%")
        summary.append(f"{icon} <b>{s['name']}</b> ({s['ipv4']})\n"
                        f"   {s['usage_percent']}% — {s['outgoing_traffic']/1024**3:.2f}/"
                        f"{s['included_traffic']/1024**3:.2f}GB")

    rebuild_results = []
    if high_traffic and auto_rebuild and snapshot_id:
        for s in high_traffic:
            add_log(f"⚠️ {s['name']} ({s['ipv4']}) 超阈值 {s['usage_percent']}%，开始重建...")

            old_name = s["name"]
            old_ip   = s["ipv4"]
            old_id   = s["id"]

            add_log(f"  [1/2] 删除旧服务器 {old_name} (id={old_id})...")
            if not hz.delete_server(old_id):
                add_log(f"  ❌ 删除旧服务器失败，跳过重建", "error")
                rebuild_results.append({"name": old_name, "ok": False, "reason": "旧服务器删除失败"})
                continue

            add_log(f"  ✅ 旧服务器已删除，等待 5s 确保名称释放...")
            time.sleep(5)

            add_log(f"  [2/2] 创建新服务器 {old_name}...")
            new_sv = hz.create_server_with_fallback(
                old_name, server_types, int(snapshot_id), ssh_keys, location
            )
            if not new_sv:
                add_log(f"  ❌ 新服务器创建失败", "error")
                rebuild_results.append({"name": old_name, "ok": False, "reason": "新服务器创建失败（旧服务器已删除）"})
                continue

            add_log(f"  ✅ 重建完成: {old_ip} → {new_sv['ip']} [{new_sv['server_type']}]  name={new_sv['name']}")
            rebuild_results.append({
                "name": old_name, "ok": True,
                "new_name": new_sv["name"],
                "old_ip": old_ip, "new_ip": new_sv["ip"],
                "server_type": new_sv["server_type"]
            })

    # 刷新服务器列表
    time.sleep(2)
    final = hz.get_servers()
    if final:
        monitor_state["servers_cache"] = [enrich_server(s) for s in final]

    # 同步 Vertex（有重建时才同步）
    if rebuild_results:
        sync_vertex_ips("流量重建后")

    # Telegram
    tg = [f"<b>🖥 Hetzner 流量报告</b>",
          f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
          f"服务器: {len(enriched)} | 超阈值: {len(high_traffic)}\n",
          *summary]
    if rebuild_results:
        tg.append("\n<b>⚙️ 重建结果:</b>")
        for r in rebuild_results:
            if r["ok"]:
                tg.append(f"✅ {r['name']}: {r.get('old_ip')} → <code>{r['new_ip']}</code> [{r['server_type']}]")
            else:
                tg.append(f"❌ {r['name']}: {r['reason']}")
    send_telegram("\n".join(tg))
    add_log("━━━ 检查完成 ━━━")


def monitor_loop(stop_event: threading.Event):
    while not stop_event.is_set():
        cfg = load_config()
        interval = int(cfg.get("check_interval", 1200))
        if cfg.get("enable_time_window"):
            now = datetime.now().time()
            start = dt_time(int(cfg.get("work_start_hour", 8)), 0)
            end = dt_time(int(cfg.get("work_end_hour", 23)), int(cfg.get("work_end_minute", 30)))
            if not (start <= now <= end):
                add_log("⏸ 非工作时段，跳过")
                stop_event.wait(timeout=60)
                continue
        try:
            do_check_and_rebuild()
        except Exception as e:
            add_log(f"监控异常: {e}", "error")
        monitor_state["next_check"] = datetime.fromtimestamp(
            datetime.now().timestamp() + interval).isoformat()
        stop_event.wait(timeout=interval)


# ─── 定时删建任务 ────────────────────────────────────────────────────────────────

def do_scheduled_delete_all():
    """定时删除所有服务器"""
    add_log("🗑️ ━━━ 定时任务：删除全部服务器 ━━━")
    hz = get_hetzner()
    if not hz:
        add_log("定时删除：未配置 API Key", "warn"); return

    servers = hz.get_servers()
    if not servers:
        add_log("定时删除：当前无服务器，跳过"); return

    add_log(f"定时删除：共 {len(servers)} 台服务器，开始删除...")
    deleted = 0
    failed_list = []
    names = [s.get("name", str(s["id"])) for s in servers]

    for s in servers:
        sid = s["id"]
        sname = s.get("name", str(sid))
        add_log(f"  删除 {sname} (id={sid})...")
        if hz.delete_server(sid):
            deleted += 1
        else:
            failed_list.append(sname)

    monitor_state["servers_cache"] = []
    monitor_state["last_scheduled_delete"] = datetime.now().isoformat()
    add_log(f"定时删除完成：成功 {deleted} / 失败 {len(failed_list)} 台")

    send_telegram(
        f"<b>🗑️ 定时删除完成</b>\n"
        f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"删除服务器: {', '.join(names)}\n"
        f"结果: ✅ {deleted} 台 / ❌ {len(failed_list)} 台失败"
        + (f"\n失败: {', '.join(failed_list)}" if failed_list else "")
    )


def do_scheduled_create():
    """定时创建若干台服务器"""
    cfg = load_config()
    add_log("🚀 ━━━ 定时任务：批量创建服务器 ━━━")

    hz = get_hetzner()
    if not hz:
        add_log("定时创建：未配置 API Key", "warn"); return

    count       = int(cfg.get("schedule_create_count", 3))
    snapshot_id = cfg.get("initial_snapshot_id", "")
    server_types = cfg.get("server_types", ["cx43"])
    ssh_keys    = cfg.get("ssh_keys", [])
    location    = cfg.get("default_location", "nbg1")
    prefix      = cfg.get("schedule_server_name_prefix", "hetzner-auto")

    if not snapshot_id:
        add_log("定时创建：未配置快照 ID，跳过", "warn"); return

    add_log(f"定时创建：准备创建 {count} 台服务器，前缀={prefix}，地区={location}")
    created_list = []
    failed = 0

    for i in range(count):
        name = f"{prefix}-{i+1:02d}"
        add_log(f"  创建第 {i+1}/{count} 台: {name} ...")
        result = hz.create_server_with_fallback(
            name, server_types, int(snapshot_id), ssh_keys, location
        )
        if result:
            created_list.append(result)
            add_log(f"  ✅ {name} → {result['ip']} [{result['server_type']}]")
        else:
            failed += 1
            add_log(f"  ❌ {name} 创建失败", "error")
        # 短暂间隔避免频繁请求
        time.sleep(2)

    # 刷新缓存
    time.sleep(3)
    final = hz.get_servers()
    if final:
        monitor_state["servers_cache"] = [enrich_server(s) for s in final]

    monitor_state["last_scheduled_create"] = datetime.now().isoformat()
    add_log(f"定时创建完成：成功 {len(created_list)} / 失败 {failed} 台")

    # 同步 Vertex
    if created_list:
        sync_vertex_ips("定时创建后")

    send_telegram(
        f"<b>🚀 定时创建完成</b>\n"
        f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"成功 {len(created_list)} / 失败 {failed} 台\n"
        + "\n".join([f"✅ <code>{s['ip']}</code> [{s['server_type']}] {s['name']}" for s in created_list])
    )


def _get_tz_now(cfg: Dict) -> datetime:
    """
    获取指定时区的当前时间。
    优先使用 pytz，若未安装则尝试 zoneinfo（Python 3.9+），
    都不可用则回退到系统本地时间并记录警告。
    """
    tz_name = cfg.get("schedule_timezone", "Asia/Shanghai").strip()
    utc_now = datetime.utcnow().replace(tzinfo=None)

    # 尝试 pytz
    try:
        import pytz
        tz = pytz.timezone(tz_name)
        return datetime.now(tz).replace(tzinfo=None)
    except Exception:
        pass

    # 尝试 zoneinfo (Python 3.9+)
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz_name)).replace(tzinfo=None)
    except Exception:
        pass

    # 回退：系统本地时间
    add_log(f"⚠️ 时区 '{tz_name}' 解析失败（需安装 pytz 或 Python≥3.9），使用系统本地时间", "warn")
    return datetime.now()


# 调度去重 key 格式：  "delete:YYYY-MM-DD HH:MM"  /  "create:YYYY-MM-DD HH:MM"
# 只要当天的「HH:MM 档」执行过就不再重复，即使调度器重启也靠 last_scheduled_* 的值校验
def _already_ran_today(task: str, target_date, target_hhmm: str) -> bool:
    """
    判断 task（'delete' 或 'create'）在 target_date 的 target_hhmm 时刻是否已执行过。
    校验依据：monitor_state['last_scheduled_{task}'] 记录的 ISO 时间字符串。
    """
    key = f"last_scheduled_{task}"
    last_iso = monitor_state.get(key)
    if not last_iso:
        return False
    try:
        last_dt = datetime.fromisoformat(last_iso)
        # 同一天 & 同一 HH:MM 档已执行 → 去重
        last_hhmm = last_dt.strftime("%H:%M")
        return last_dt.date() == target_date and last_hhmm == target_hhmm
    except Exception:
        return False


def scheduler_loop(stop_event: threading.Event):
    """
    每 30 秒唤醒一次，以指定时区的当前时间判断是否到达设定时刻。

    去重策略：
      - 以「日期 + HH:MM」为单位，同一时刻当天只执行一次。
      - 与调度器唤醒节奏无关——即使某次唤醒偏移了几秒也不会漏触发或重复触发。

    时间匹配窗口：
      - 当 now.minute == target_minute AND now.hour == target_hour 时触发。
      - 不依赖秒级精度，30 秒唤醒间隔在同一分钟内至多触发一次（靠去重保证）。
    """
    add_log("⏰ 定时任务调度器已启动")
    from datetime import timedelta

    while not stop_event.is_set():
        # 每 30 秒检查一次，确保在目标分钟内至少唤醒一次
        stop_event.wait(timeout=30)
        if stop_event.is_set():
            break

        cfg = load_config()
        if not cfg.get("scheduled_tasks_enabled"):
            continue

        now = _get_tz_now(cfg)          # 目标时区的当前时间
        today = now.date()
        now_hhmm = now.strftime("%H:%M")

        # ── 定时删除 ──────────────────────────────────────────────
        if cfg.get("schedule_delete_enabled"):
            dh = int(cfg.get("schedule_delete_hour", 23))
            dm = int(cfg.get("schedule_delete_minute", 0))
            target_hhmm = f"{dh:02d}:{dm:02d}"

            if now.hour == dh and now.minute == dm:
                if not _already_ran_today("delete", today, target_hhmm):
                    add_log(f"⏰ 触发定时删除（{cfg.get('schedule_timezone','本地')} {now_hhmm}）")
                    # 先标记，防止任务执行中途调度器再次检查时重入
                    monitor_state["last_scheduled_delete"] = now.isoformat()
                    # 计算下次执行时间（次日同时刻）
                    next_del = now.replace(hour=dh, minute=dm, second=0, microsecond=0) + timedelta(days=1)
                    monitor_state["next_scheduled_delete"] = next_del.isoformat()
                    try:
                        do_scheduled_delete_all()
                    except Exception as e:
                        add_log(f"定时删除异常: {e}", "error")

        # ── 定时创建 ──────────────────────────────────────────────
        if cfg.get("schedule_create_enabled"):
            ch = int(cfg.get("schedule_create_hour", 8))
            cm = int(cfg.get("schedule_create_minute", 0))
            target_hhmm = f"{ch:02d}:{cm:02d}"

            if now.hour == ch and now.minute == cm:
                if not _already_ran_today("create", today, target_hhmm):
                    add_log(f"⏰ 触发定时创建（{cfg.get('schedule_timezone','本地')} {now_hhmm}）")
                    monitor_state["last_scheduled_create"] = now.isoformat()
                    next_cre = now.replace(hour=ch, minute=cm, second=0, microsecond=0) + timedelta(days=1)
                    monitor_state["next_scheduled_create"] = next_cre.isoformat()
                    try:
                        do_scheduled_create()
                    except Exception as e:
                        add_log(f"定时创建异常: {e}", "error")

    add_log("⏰ 定时任务调度器已停止")


def start_scheduler():
    if monitor_state["scheduler_running"]:
        return
    monitor_state["scheduler_stop_event"].clear()
    threading.Thread(
        target=scheduler_loop,
        args=(monitor_state["scheduler_stop_event"],),
        daemon=True, name="scheduler"
    ).start()
    monitor_state["scheduler_running"] = True
    add_log("⏰ 定时调度器启动")


def stop_scheduler():
    monitor_state["scheduler_stop_event"].set()
    monitor_state["scheduler_running"] = False
    add_log("⏰ 定时调度器停止")


# ─── Auth ──────────────────────────────────────────────────────────────────────
def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("authenticated"):
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper


# ═══════════════════════════════════════════════════════════════════════════════
# Routes
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/login", methods=["POST"])
def login():
    data = request.json or {}
    cfg = load_config()
    if data.get("password") == cfg.get("web_password", "admin123"):
        session["authenticated"] = True
        session.permanent = True
        return jsonify({"success": True})
    return jsonify({"error": "密码错误"}), 401

@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"success": True})

@app.route("/api/auth/status")
def auth_status():
    return jsonify({"authenticated": bool(session.get("authenticated"))})

# ─── Config ────────────────────────────────────────────────────────────────────
@app.route("/api/config", methods=["GET"])
@require_auth
def get_config():
    cfg = load_config()
    safe = {k: v for k, v in cfg.items() if k != "web_password"}
    for field in ("hetzner_api_key", "telegram_bot_token"):
        if safe.get(field):
            v = safe[field]
            safe[f"{field}_masked"] = v[:8] + "..." + v[-4:] if len(v) > 12 else "***"
            safe[field] = ""
    if safe.get("vertex_cookies"):
        safe["vertex_cookies_set"] = True
        safe["vertex_cookies"] = ""
    return jsonify(safe)

@app.route("/api/config", methods=["POST"])
@require_auth
def update_config():
    data = request.json or {}
    cfg = load_config()
    sensitive = ("hetzner_api_key", "telegram_bot_token", "vertex_cookies")
    for k in DEFAULT_CONFIG.keys():
        if k in data:
            if k in sensitive and data[k] == "":
                continue
            cfg[k] = data[k]
    save_config(cfg)
    add_log("⚙️ 配置已更新")
    # 若定时任务总开关变动，联动启停
    if "scheduled_tasks_enabled" in data:
        if data["scheduled_tasks_enabled"]:
            start_scheduler()
        else:
            stop_scheduler()
    return jsonify({"success": True})

@app.route("/api/config/server-type-catalog")
@require_auth
def server_type_catalog():
    return jsonify({"catalog": SERVER_TYPE_CATALOG})

# ─── Servers ──────────────────────────────────────────────────────────────────
@app.route("/api/servers")
@require_auth
def list_servers():
    force = request.args.get("refresh") == "1"
    if force or not monitor_state["servers_cache"]:
        hz = get_hetzner()
        if hz:
            monitor_state["servers_cache"] = [enrich_server(s) for s in hz.get_servers()]
            monitor_state["last_check"] = datetime.now().isoformat()
    return jsonify({"servers": monitor_state["servers_cache"],
                    "last_check": monitor_state["last_check"],
                    "count": len(monitor_state["servers_cache"])})

@app.route("/api/servers/<int:server_id>", methods=["DELETE"])
@require_auth
def delete_server(server_id):
    hz = get_hetzner()
    if not hz:
        return jsonify({"error": "API Key 未配置"}), 400
    add_log(f"🗑️ 删除服务器 {server_id}...")
    if hz.delete_server(server_id):
        monitor_state["servers_cache"] = [s for s in monitor_state["servers_cache"] if s["id"] != server_id]
        add_log(f"✅ 已删除 {server_id}")
        return jsonify({"success": True})
    return jsonify({"error": "删除失败"}), 500

@app.route("/api/servers/create", methods=["POST"])
@require_auth
def create_server():
    data = request.json or {}
    hz = get_hetzner()
    if not hz:
        return jsonify({"error": "API Key 未配置"}), 400
    cfg = load_config()
    image_id = data.get("image_id") or cfg.get("initial_snapshot_id")
    if not image_id:
        return jsonify({"error": "未指定镜像 ID"}), 400
    server_types = [data["server_type"]] if data.get("server_type") else cfg.get("server_types", ["cx43"])
    name_raw = data.get("name", f"server-{int(time.time())}")
    name = HetznerAPI.sanitize_name(name_raw)
    location = data.get("location", cfg.get("default_location", "nbg1"))
    result = hz.create_server_with_fallback(name, server_types, int(image_id),
                                             data.get("ssh_keys", cfg.get("ssh_keys", [])), location)
    if not result:
        return jsonify({"error": "创建失败，所有型号无货"}), 500

    # 刷新缓存
    time.sleep(2)
    fresh = hz.get_servers()
    if fresh:
        monitor_state["servers_cache"] = [enrich_server(s) for s in fresh]

    # ── 修复：手动创建后立即同步 Vertex ──
    sync_vertex_ips("手动创建服务器后")

    return jsonify({"success": True, "server": result})

@app.route("/api/servers/rebuild/<int:server_id>", methods=["POST"])
@require_auth
def rebuild_server(server_id):
    hz = get_hetzner()
    if not hz:
        return jsonify({"error": "API Key 未配置"}), 400
    cfg = load_config()
    target = next((s for s in monitor_state["servers_cache"] if s["id"] == server_id), None)
    if not target:
        raw = hz.get_server(server_id)
        target = enrich_server(raw) if raw else None
    if not target:
        return jsonify({"error": "服务器不存在"}), 404
    img_id = target["image"]["id"] if target["image"].get("type") == "snapshot" else None
    if not img_id and cfg.get("initial_snapshot_id"):
        img_id = int(cfg["initial_snapshot_id"])
    if not img_id:
        return jsonify({"error": "无可用快照"}), 400

    old_name = target["name"]
    old_ip   = target["ipv4"]
    server_types = cfg.get("server_types", ["cx43"])
    location = cfg.get("default_location", "nbg1")
    ssh_keys = cfg.get("ssh_keys", [])

    add_log(f"🔄 手动重建 {old_name} ({old_ip})...")

    add_log(f"  [1/2] 删除旧服务器 {old_name} (id={server_id})...")
    if not hz.delete_server(server_id):
        return jsonify({"error": "旧服务器删除失败，重建取消"}), 500

    add_log(f"  等待 5s 确保名称释放...")
    time.sleep(5)

    add_log(f"  [2/2] 创建新服务器 {old_name}...")
    new_sv = hz.create_server_with_fallback(old_name, server_types, img_id, ssh_keys, location)
    if not new_sv:
        return jsonify({"error": "旧服务器已删除，但新服务器创建失败，请手动创建"}), 500

    add_log(f"✅ 手动重建完成: {old_ip} → {new_sv['ip']} [{new_sv['server_type']}] name={new_sv['name']}")

    time.sleep(2)
    monitor_state["servers_cache"] = [enrich_server(s) for s in hz.get_servers()]

    # 同步 Vertex
    sync_vertex_ips("手动重建后")

    return jsonify({"success": True, "new_server": new_sv, "old_ip": old_ip})

# ─── Images ───────────────────────────────────────────────────────────────────
@app.route("/api/images")
@require_auth
def list_images():
    hz = get_hetzner()
    if not hz:
        return jsonify({"error": "API Key 未配置"}), 400
    images = hz.get_images(request.args.get("type", "snapshot"))
    return jsonify({"images": [{
        "id": i["id"], "name": i.get("name") or i.get("description", ""),
        "description": i.get("description", ""), "type": i.get("type", ""),
        "status": i.get("status", ""), "created": i.get("created", ""),
        "disk_size": i.get("disk_size", 0), "image_size": i.get("image_size"),
        "os_flavor": i.get("os_flavor", ""), "os_version": i.get("os_version", ""),
        "labels": i.get("labels", {})
    } for i in images], "count": len(images)})

# ─── SSH Keys ─────────────────────────────────────────────────────────────────
@app.route("/api/ssh-keys")
@require_auth
def list_ssh_keys():
    hz = get_hetzner()
    if not hz:
        return jsonify({"error": "API Key 未配置"}), 400
    return jsonify({"ssh_keys": [{"id": k["id"], "name": k["name"]} for k in hz.get_ssh_keys()]})

# ─── Monitor ──────────────────────────────────────────────────────────────────
@app.route("/api/monitor/status")
@require_auth
def monitor_status():
    return jsonify({
        "running": monitor_state["running"],
        "last_check": monitor_state["last_check"],
        "next_check": monitor_state["next_check"],
        "scheduler_running": monitor_state["scheduler_running"],
        "last_scheduled_delete": monitor_state["last_scheduled_delete"],
        "last_scheduled_create": monitor_state["last_scheduled_create"],
        "next_scheduled_delete": monitor_state["next_scheduled_delete"],
        "next_scheduled_create": monitor_state["next_scheduled_create"],
    })

@app.route("/api/monitor/start", methods=["POST"])
@require_auth
def start_monitor():
    if monitor_state["running"]:
        return jsonify({"message": "已在运行"})
    monitor_state["stop_event"].clear()
    threading.Thread(target=monitor_loop, args=(monitor_state["stop_event"],), daemon=True).start()
    monitor_state["running"] = True
    add_log("▶️ 监控已启动")
    return jsonify({"success": True})

@app.route("/api/monitor/stop", methods=["POST"])
@require_auth
def stop_monitor():
    monitor_state["stop_event"].set()
    monitor_state["running"] = False
    add_log("⏹️ 监控已停止")
    return jsonify({"success": True})

@app.route("/api/monitor/check-now", methods=["POST"])
@require_auth
def check_now():
    threading.Thread(target=lambda: _safe(do_check_and_rebuild), daemon=True).start()
    return jsonify({"success": True})

def _safe(fn):
    try:
        fn()
    except Exception as e:
        add_log(f"执行异常: {e}", "error")

@app.route("/api/monitor/logs")
@require_auth
def get_logs():
    return jsonify({"logs": monitor_state["logs"][:int(request.args.get("limit", 150))]})

# ─── 定时任务 API ─────────────────────────────────────────────────────────────
@app.route("/api/scheduler/trigger-delete", methods=["POST"])
@require_auth
def trigger_delete():
    """手动立即触发定时删除"""
    threading.Thread(target=lambda: _safe(do_scheduled_delete_all), daemon=True).start()
    return jsonify({"success": True, "message": "定时删除任务已触发"})

@app.route("/api/scheduler/trigger-create", methods=["POST"])
@require_auth
def trigger_create():
    """手动立即触发定时创建"""
    threading.Thread(target=lambda: _safe(do_scheduled_create), daemon=True).start()
    return jsonify({"success": True, "message": "定时创建任务已触发"})

# ─── Vertex ───────────────────────────────────────────────────────────────────
@app.route("/api/vertex/test", methods=["POST"])
@require_auth
def vertex_test():
    vertex = get_vertex()
    if not vertex:
        return jsonify({"error": "Vertex 未配置 URL 或 Cookies"}), 400
    ok, msg = vertex.test_connection()
    return jsonify({"success": ok, "message": msg})

@app.route("/api/vertex/refresh-cookie", methods=["POST"])
@require_auth
def vertex_refresh_cookie():
    global _vcm_instance
    vcm = _build_vcm()
    if not vcm:
        return jsonify({"error": "未配置 vertex_username / vertex_password_md5，无法自动刷新"}), 400
    new_cookie = vcm.force_refresh()
    if not new_cookie:
        return jsonify({"error": "登录失败，请检查 Vertex 地址和密码"}), 500
    cfg = load_config()
    cfg["vertex_cookies"] = new_cookie
    save_config(cfg)
    add_log(f"🔑 [VTCookie] 手动刷新成功: {new_cookie[:40]}...")
    return jsonify({"success": True, "cookie_preview": new_cookie[:40] + "..."})

@app.route("/api/vertex/downloaders")
@require_auth
def vertex_list_downloaders():
    vertex = get_vertex()
    if not vertex:
        return jsonify({"error": "Vertex 未配置"}), 400
    all_dl = vertex.get_all_downloaders()
    keyword = load_config().get("vertex_downloader_keyword", "Hetzner")
    for dl in all_dl:
        dl["_is_hetzner"] = keyword.lower() in dl.get("alias", "").lower()
        dl["_current_ip"] = vertex._extract_ip(dl.get("clientUrl", "") or "")
    return jsonify({"downloaders": all_dl, "count": len(all_dl)})

@app.route("/api/vertex/sync", methods=["POST"])
@require_auth
def vertex_sync_now():
    vertex = get_vertex()
    if not vertex:
        return jsonify({"error": "Vertex 未配置"}), 400

    data = request.json or {}
    server_ips = data.get("ips")

    # ── 修复：始终从 API 实时拉取最新 IP，不依赖缓存 ──
    if not server_ips:
        hz = get_hetzner()
        if hz:
            raw_servers = hz.get_servers()
            # 刷新缓存
            if raw_servers:
                monitor_state["servers_cache"] = [enrich_server(s) for s in raw_servers]
            server_ips = [
                (s.get("public_net", {}).get("ipv4") or {}).get("ip", "")
                for s in raw_servers
                if s.get("status") == "running"
            ]
            server_ips = [ip for ip in server_ips if ip]

    # 若 API 也没拿到，再尝试缓存
    if not server_ips:
        server_ips = [s["ipv4"] for s in monitor_state["servers_cache"] if s.get("ipv4")]

    if not server_ips:
        return jsonify({"error": "无可用服务器 IP，请先创建服务器"}), 400

    add_log(f"🔄 手动 Vertex 同步，IP: {server_ips}")
    result = vertex.sync_with_server_ips(server_ips)
    return jsonify({"success": True, "result": result, "ips_used": server_ips})

# ─── Telegram ─────────────────────────────────────────────────────────────────
@app.route("/api/telegram/test", methods=["POST"])
@require_auth
def test_telegram():
    send_telegram(f"✅ <b>Hetzner Web Manager</b>\nTelegram 测试成功\n{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return jsonify({"success": True})

# ─── Static ────────────────────────────────────────────────────────────────────
@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve(path):
    if path and os.path.exists(os.path.join("static", path)):
        return send_from_directory("static", path)
    return send_from_directory("static", "index.html")


if __name__ == "__main__":
    os.makedirs("static", exist_ok=True)
    port = int(os.getenv("PORT", 8080))
    logger.info(f"🚀 Hetzner Web Manager on :{port}")

    # 若配置了定时任务总开关，启动时自动启动调度器
    cfg = load_config()
    if cfg.get("scheduled_tasks_enabled"):
        start_scheduler()

    app.run(host="0.0.0.0", port=port, debug=False)