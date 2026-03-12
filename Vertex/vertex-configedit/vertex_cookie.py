"""
vertex_cookie.py — Vertex 下载器 Cookie 自动刷新模块
=====================================================
用法示例：
    from vertex_cookie import VertexCookieManager

    # 传入明文密码（推荐）
    vcm = VertexCookieManager(
        login_url = "",
        username  = "admin",
        password  = "",  # 明文密码，会自动 MD5
    )
    # 或传入已知 MD5 就直接用：
    # vcm = VertexCookieManager(..., password="2715...", password_is_hashed=True)

    cookie_str = vcm.get_valid_cookie()
    headers = {"Cookie": cookie_str}
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ── 缓存文件固定在本文件同目录 ──────────────────────────────────────────
_MODULE_DIR  = os.path.dirname(os.path.abspath(__file__))
_CACHE_FILE  = os.path.join(_MODULE_DIR, "vertex_cookie_cache.json")
_CACHE_LOCK  = threading.Lock()   # 进程内多线程写保护（跨进程靠文件锁）


class VertexCookieManager:
    """
    Vertex Cookie 管理器。

    参数
    ----
    login_url      : Vertex 服务地址，例如 "http://23.82.99.203:3077"
    username       : 登录用户名
    password       : 登录密码。默认为明文，程序会自动 MD5 后发送。
                     如果传入的已是 MD5 哈希字符串，请将 password_is_hashed 设为 True。
    password_is_hashed : 为 True 时跟登录接口 payload 直接发送（不再二次 hash）
    check_interval : Cookie 有效性探测最小间隔（秒）
    timeout        : 请求超时（秒）
    """

    def __init__(
        self,
        login_url: str,
        username: str,
        password: str = None,          # 密码（支持明文或 MD5）
        password_is_hashed: bool = False,
        check_interval: int = 300,
        timeout: int = 10,
        password_md5: str = None,      # 兼容旧代码名
    ):
        self.login_url         = login_url.rstrip("/")
        self.username          = username
        self.check_interval    = check_interval
        self.timeout           = timeout
        self._lock             = threading.Lock()

        # 处理参数：password 优先，password_md5 次之
        raw_pwd = (password or password_md5 or "").strip()
        if not raw_pwd:
            raise ValueError("[VertexCookie] 必须提供 password (明文密码或 MD5 字符串)")

        # 密码加密逻辑：
        # 1. 如果显式指定 password_is_hashed=True，直接使用且转为小写。
        # 2. 如果未指定，但 raw_pwd 本身就是 32 位十六进制（符合 MD5 特征），则直接作为 MD5 使用。
        # 3. 否则，将其视为明文，加密为 32 位小写 MD5 字符。
        
        is_md5_hex = len(raw_pwd) == 32 and all(c in "0123456789abcdefABCDEF" for c in raw_pwd)

        if password_is_hashed:
            self._password_md5 = raw_pwd.lower()
        elif is_md5_hex:
            # 启发式识别：如果刚好是 32 位 hex，很大可能是用户已经提供了 MD5
            self._password_md5 = raw_pwd.lower()
            logger.debug(f"[VertexCookie] 检测到 32 位十六进制字符串，视为已哈希密码")
        else:
            # 视为明文，进行 MD5 加密
            self._password_md5 = hashlib.md5(raw_pwd.encode("utf-8")).hexdigest()
            logger.info(f"[VertexCookie] 提供的密码已自动加密为 MD5")

        logger.debug(f"[VertexCookie] 初始化成功: url={self.login_url}, user={username}, md5_tail={self._password_md5[-6:]}")

    # ── 缓存读写 ─────────────────────────────────────────────────────
    @staticmethod
    def _read_cache() -> dict:
        with _CACHE_LOCK:
            try:
                if os.path.exists(_CACHE_FILE):
                    with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                        return json.load(f)
            except Exception as e:
                logger.warning(f"[VertexCookie] 读取缓存失败: {e}")
        return {}

    @staticmethod
    def _write_cache(data: dict):
        with _CACHE_LOCK:
            try:
                with open(_CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
            except Exception as e:
                logger.warning(f"[VertexCookie] 写入缓存失败: {e}")

    def _cache_key(self) -> str:
        """用 login_url + username 做缓存 key，支持多账号/多节点"""
        return f"{self.login_url}::{self.username}"

    def _get_cached_cookie(self) -> Optional[str]:
        cache = self._read_cache()
        entry = cache.get(self._cache_key(), {})
        return entry.get("cookie")

    def _save_cookie(self, cookie: str):
        cache = self._read_cache()
        cache[self._cache_key()] = {
            "cookie":     cookie,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._write_cache(cache)
        logger.info(f"[VertexCookie] Cookie 已更新并缓存: {cookie[:40]}...")

    def _get_last_check_time(self) -> float:
        cache = self._read_cache()
        entry = cache.get(self._cache_key(), {})
        return float(entry.get("last_check", 0))

    def _save_last_check_time(self):
        cache = self._read_cache()
        key   = self._cache_key()
        if key not in cache:
            cache[key] = {}
        cache[key]["last_check"] = time.time()
        self._write_cache(cache)

    # ── 登录 ─────────────────────────────────────────────────────────
    def login(self) -> Optional[str]:
        """
        向 Vertex 发起登录请求，成功返回 "connect.sid=xxxx" Cookie 字符串，
        失败返回 None。
        """
        url = f"{self.login_url}/api/user/login"
        payload = {
            "username": self.username,
            "password": self._password_md5,   # 内部统一使用 MD5 化后的密码
            "otpPw":    "",
        }
        try:
            resp = requests.post(url, json=payload, timeout=self.timeout)
            # 优先从 Set-Cookie 提取 connect.sid
            raw_cookies = resp.headers.get("Set-Cookie", "")
            cookie_str  = self._extract_connect_sid(raw_cookies)

            if not cookie_str:
                # 部分版本放在 JSON body 里
                try:
                    body = resp.json()
                    sid  = body.get("data", {}).get("sid") or body.get("sid")
                    if sid:
                        cookie_str = f"connect.sid={sid}"
                except Exception:
                    pass

            if cookie_str:
                logger.info(f"[VertexCookie] 登录成功，新 Cookie 已获取")
                self._save_cookie(cookie_str)
                return cookie_str

            logger.warning(
                f"[VertexCookie] 登录成功但未提取到 Cookie，"
                f"HTTP {resp.status_code}, body={resp.text[:200]}"
            )
            return None

        except Exception as e:
            logger.error(f"[VertexCookie] 登录请求异常: {e}")
            return None

    @staticmethod
    def _extract_connect_sid(set_cookie_header: str) -> Optional[str]:
        """从 Set-Cookie 响应头中提取 connect.sid=xxx 部分"""
        if not set_cookie_header:
            return None
        for part in set_cookie_header.split(","):
            part = part.strip()
            # 每段 Set-Cookie 以 name=value 开头，";" 分隔属性
            first = part.split(";")[0].strip()
            if first.lower().startswith("connect.sid="):
                return first   # "connect.sid=s%3Axxx..."
        return None

    # ── 有效性探测 ───────────────────────────────────────────────────
    def is_cookie_valid(self, cookie: str) -> bool:
        """
        向 /api/downloader/list 发一次探测请求，
        401 / 403 或 JSON 中包含 "未登录"/"Unauthorized" 视为失效。
        """
        try:
            r = requests.get(
                f"{self.login_url}/api/downloader/list",
                headers={"Cookie": cookie},
                timeout=self.timeout,
            )
            if r.status_code in (401, 403):
                return False
            # 部分 Vertex 实现在 200 里返回错误 JSON
            if r.status_code == 200:
                try:
                    body = r.json()
                    msg  = str(body.get("message", "") or body.get("msg", "")).lower()
                    if "未登录" in msg or "unauthorized" in msg or "login" in msg:
                        return False
                except Exception:
                    pass
                return True
            # 其他非 2xx 状态码保守认为失效
            return False
        except Exception as e:
            logger.warning(f"[VertexCookie] 有效性探测异常: {e}")
            return False

    # ── 核心：获取有效 Cookie ────────────────────────────────────────
    def get_valid_cookie(self, force_refresh: bool = False) -> str:
        """
        获取一个有效的 Vertex Cookie 字符串（自动检测 + 刷新）。

        逻辑：
        1. 读取缓存 Cookie
        2. 若距上次探测未超 check_interval，直接返回缓存（减少请求）
        3. 探测 Cookie 是否有效，有效直接返回
        4. 无效则登录获取新 Cookie 并缓存
        5. 若登录也失败，返回旧缓存（降级，总比空好）
        """
        with self._lock:
            cached = self._get_cached_cookie()

            # 短路：未到探测周期，直接复用
            if not force_refresh and cached:
                elapsed = time.time() - self._get_last_check_time()
                if elapsed < self.check_interval:
                    return cached

            # 探测有效性
            if cached and not force_refresh and self.is_cookie_valid(cached):
                self._save_last_check_time()
                logger.debug("[VertexCookie] Cookie 仍有效，跳过刷新")
                return cached

            # 需要刷新
            logger.info("[VertexCookie] Cookie 失效或强制刷新，正在重新登录...")
            new_cookie = self.login()
            self._save_last_check_time()

            if new_cookie:
                return new_cookie
            if cached:
                logger.warning("[VertexCookie] 登录失败，降级使用旧 Cookie")
                return cached

            logger.error("[VertexCookie] 无可用 Cookie，请检查 Vertex 配置")
            return ""

    def refresh_if_needed(self) -> str:
        """与 get_valid_cookie 等价的别名，语义更清晰"""
        return self.get_valid_cookie()

    def force_refresh(self) -> str:
        """强制重新登录获取新 Cookie，忽略探测周期"""
        return self.get_valid_cookie(force_refresh=True)

# ── 便捷工厂：从环境变量构造 ─────────────────────────────────────────
def from_env(
    url_env:      str = "VTURL",
    user_env:     str = "VT_USERNAME",
    pwd_env:      str = "VT_PASSWORD",       # 明文密码（优先，会自动 MD5）
    pwd_md5_env:  str = "VT_PASSWORD_MD5",   # MD5 密码（次优先）
    fallback_url: str = "",
) -> Optional[VertexCookieManager]:
    """
    从环境变量构造 VertexCookieManager。
    优先级：VT_PASSWORD（明文，自动 MD5）> VT_PASSWORD_MD5（直接使用）
    返回 None 表示环境变量未配置。
    """
    login_url = os.getenv(url_env, fallback_url).rstrip("/")
    username  = os.getenv(user_env, "")
    if not (login_url and username):
        return None
    plain = os.getenv(pwd_env, "")
    if plain:
        return VertexCookieManager(login_url, username, plain, password_is_hashed=False)
    hashed = os.getenv(pwd_md5_env, "")
    if hashed:
        return VertexCookieManager(login_url, username, hashed, password_is_hashed=True)
    return None


def md5_password(plaintext: str) -> str:
    """将明文密码转换为 MD5（与 Vertex 前端行为一致）

    示例：
        >>> md5_password("713aa2ac-ddd5-403a-9ddd-413255289a")
        # 返回对应的 32 位小写 MD5 字符串
    """
    return hashlib.md5(plaintext.encode("utf-8")).hexdigest()
