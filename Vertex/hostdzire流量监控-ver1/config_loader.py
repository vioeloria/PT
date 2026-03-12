"""
config_loader.py — 配置热重载模块

使用方式：
    from config_loader import cfg

    cfg.TG_BOT_TOKEN          # 全局属性
    cfg.account_products()    # 账号衍生结构
    cfg.get("global", "push_interval", default=1800)  # 链式取值

文件变动检测：
    每次访问任意属性/方法时，自动比对 mtime，文件有变动则立即重新加载。
    加载失败时保留旧配置，并写入日志，不会崩溃主进程。

手动强制重载：
    cfg.reload()
"""

from __future__ import annotations

import os
import threading
import logging
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# 配置文件路径：优先读取环境变量 MONITOR_CONFIG，默认 ./config.yaml
CONFIG_PATH = os.environ.get("MONITOR_CONFIG", "./config.yaml")


class ConfigLoader:
    def __init__(self, path: str = CONFIG_PATH):
        self._path = path
        self._lock = threading.RLock()
        self._mtime: float = 0.0
        self._config: dict = {}
        self._load()

    # ── 内部加载 ──────────────────────────────────────────────

    def _load(self) -> None:
        try:
            mtime = os.path.getmtime(self._path)
            with open(self._path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            with self._lock:
                self._config = data or {}
                self._mtime = mtime
            logger.info(f"[Config] 配置已加载: {self._path}")
        except Exception as e:
            logger.error(f"[Config] 加载失败（保留旧配置）: {e}")

    def _maybe_reload(self) -> None:
        """检查文件 mtime，有变动则热重载"""
        try:
            mtime = os.path.getmtime(self._path)
            if mtime != self._mtime:
                logger.info("[Config] 检测到文件变动，热重载中...")
                self._load()
        except OSError:
            pass

    # ── 公开接口 ──────────────────────────────────────────────

    def get(self, *keys: str, default: Any = None) -> Any:
        """
        链式取值，例如：
            cfg.get("global", "push_interval", default=1800)
        """
        self._maybe_reload()
        with self._lock:
            node = self._config
            for k in keys:
                if not isinstance(node, dict):
                    return default
                node = node.get(k)
                if node is None:
                    return default
            return node

    def reload(self) -> None:
        """手动强制重载配置文件"""
        self._load()

    @property
    def raw(self) -> dict:
        """返回当前完整配置字典的浅拷贝"""
        self._maybe_reload()
        with self._lock:
            return dict(self._config)

    # ── 全局属性 ──────────────────────────────────────────────

    @property
    def TG_BOT_TOKEN(self) -> str:
        return self.get("global", "tg_bot_token", default="")

    @property
    def TARGET_CHAT_IDS(self) -> list[int]:
        return self.get("global", "target_chat_ids", default=[])

    @property
    def COOKIE_DIR(self) -> str:
        return self.get("global", "cookie_dir", default="./cookies")

    @property
    def PUSH_INTERVAL(self) -> int:
        return int(self.get("global", "push_interval", default=1800))

    @property
    def TRAFFIC_THRESHOLD_TB(self) -> float:
        return float(self.get("global", "traffic_threshold_tb", default=20.0))

    @property
    def QB_ANNOUNCE_WAIT_SECONDS(self) -> int:
        return int(self.get("global", "qb_announce_wait_seconds", default=10))

    @property
    def QB_TIMEOUT(self) -> int:
        return int(self.get("global", "qb_timeout", default=30))

    # ── Vertex 属性 ───────────────────────────────────────────

    @property
    def VERTEX_LOGIN_URL(self) -> str:
        return self.get("vertex", "login_url", default="")

    @property
    def VERTEX_USERNAME(self) -> str:
        return self.get("vertex", "username", default="")

    @property
    def VERTEX_PASSWORD(self) -> str:
        return self.get("vertex", "password", default="")

    # ── 账号列表（原始） ─────────────────────────────────────

    def accounts(self) -> list[dict]:
        """返回 accounts 列表，每次调用都会触发热重载检测"""
        return self.get("accounts", default=[])

    # ── 账号衍生结构（替换原四个大字典） ────────────────────

    def account_cookie_map(self) -> dict[str, str]:
        """alias -> cookie_file"""
        return {a["alias"]: a["cookie_file"] for a in self.accounts()}

    def account_products(self) -> dict[str, list[dict]]:
        """alias -> [{"id": ..., "type": ...}, ...]"""
        return {a["alias"]: a.get("products", []) for a in self.accounts()}

    def traffic_thresholds_tb(self) -> dict[str, float]:
        """alias -> threshold_tb (float)"""
        return {a["alias"]: float(a.get("traffic_threshold_tb", self.TRAFFIC_THRESHOLD_TB))
                for a in self.accounts()}

    def account_vertex_ip_map(self) -> dict[str, list[str]]:
        """alias -> [ip, ...]"""
        return {a["alias"]: a.get("vertex_ips", []) for a in self.accounts()}

    def account_qb_override(self) -> dict[str, str]:
        """alias -> qb_override_url（仅非空条目）"""
        return {
            a["alias"]: a["qb_override"]
            for a in self.accounts()
            if a.get("qb_override")
        }


# ── 全局单例 ──────────────────────────────────────────────────
cfg = ConfigLoader()
