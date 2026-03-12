"""
Traffic Monitor for hostdzire.com
- lswVPS (Leaseweb 分销机器) + AVM (自营机房) 双类型支持
- 定时每30分钟推送流量信息到指定 Telegram Chat ID
- 超阈值后：
    1. 通过 qBittorrent Web API（经 Vertex 代理）把所有种子全部汇报后删除（含文件）
    2. 通过 Vertex API 禁用对应下载器
    3. 推送详细操作通知到 Telegram

配置文件：config.yaml（支持热重载，修改后无需重启）
"""

from __future__ import annotations

import os
import re
import time
import asyncio
import logging
from datetime import datetime
from typing import Optional

import requests
from telegram import Bot

from config_loader import cfg          # 热重载配置单例
from vertex_cookie import VertexCookieManager

logger = logging.getLogger(__name__)

BASE_API = "https://hostdzire.com/billing/modules/servers/lswVPS/api.php"
AVM_API  = "https://hostdzire.com/billing/index.php"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://hostdzire.com/billing/clientarea.php",
    "Origin": "https://hostdzire.com",
}


# ============================================================
# Vertex Cookie Manager（单例，跟随配置热重载）
# ============================================================

_vertex_cookie_manager: VertexCookieManager | None = None
_vertex_manager_key: tuple = ()   # 记录上次初始化时用的配置，配置变动时重建


def get_vertex_manager() -> VertexCookieManager:
    """
    返回 Vertex Cookie Manager 单例。
    若 Vertex 相关配置发生变动，自动重建实例。
    """
    global _vertex_cookie_manager, _vertex_manager_key

    current_key = (cfg.VERTEX_LOGIN_URL, cfg.VERTEX_USERNAME, cfg.VERTEX_PASSWORD)
    if _vertex_cookie_manager is None or current_key != _vertex_manager_key:
        _vertex_cookie_manager = VertexCookieManager(
            login_url=cfg.VERTEX_LOGIN_URL,
            username=cfg.VERTEX_USERNAME,
            password=cfg.VERTEX_PASSWORD,
        )
        _vertex_manager_key = current_key
        logger.info("[Vertex] Cookie Manager 已（重）初始化")

    return _vertex_cookie_manager


# ============================================================
# qBittorrent 控制器（经 Vertex 代理）
# ============================================================

class QBittorrentController:
    """
    通过 Vertex 代理路径操作 qBittorrent Web API。
    超时从 cfg.QB_TIMEOUT 动态读取。
    """

    def __init__(self, proxy_base_url: str):
        self.base = proxy_base_url.rstrip("/") + "/"

    @property
    def timeout(self) -> int:
        return cfg.QB_TIMEOUT

    def _headers(self) -> dict:
        cookie_str = get_vertex_manager().get_valid_cookie()
        return {
            "Cookie": cookie_str,
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": self.base,
        }

    def _get(self, path: str, **kwargs) -> requests.Response:
        url = self.base + path.lstrip("/")
        return requests.get(url, headers=self._headers(), timeout=self.timeout, **kwargs)

    def _post(self, path: str, data: dict | None = None, **kwargs) -> requests.Response:
        url = self.base + path.lstrip("/")
        return requests.post(url, data=data, headers=self._headers(), timeout=self.timeout, **kwargs)

    # ── 基础操作 ──────────────────────────────────────────────

    def get_version(self) -> str:
        try:
            r = self._get("api/v2/app/version")
            r.raise_for_status()
            return r.text.strip()
        except Exception as e:
            raise RuntimeError(f"无法连接 qBittorrent: {e}") from e

    def get_all_torrents(self) -> list[dict]:
        r = self._get("api/v2/torrents/info")
        r.raise_for_status()
        return r.json()

    def reannounce_all(self) -> bool:
        try:
            r = self._post("api/v2/torrents/reannounce", data={"hashes": "all"})
            r.raise_for_status()
            return True
        except Exception as e:
            logger.warning(f"[QB] reannounce 失败: {e}")
            return False

    def delete_all(self, delete_files: bool = True) -> bool:
        try:
            r = self._post("api/v2/torrents/delete", data={
                "hashes": "all",
                "deleteFiles": "true" if delete_files else "false",
            })
            r.raise_for_status()
            return True
        except Exception as e:
            logger.warning(f"[QB] delete_all 失败: {e}")
            return False

    def pause_all(self) -> bool:
        try:
            r = self._post("api/v2/torrents/pause", data={"hashes": "all"})
            r.raise_for_status()
            return True
        except Exception as e:
            logger.warning(f"[QB] pause_all 失败: {e}")
            return False

    # ── 汇报后删除主流程 ──────────────────────────────────────

    def announce_and_delete_all(self) -> dict:
        """
        完整流程：
          1. 连通性检查
          2. 获取种子数量
          3. 暂停所有种子
          4. Reannounce（向 Tracker 汇报 stopped）
          5. 等待 QB_ANNOUNCE_WAIT_SECONDS 秒
          6. 删除所有种子（含文件）
        """
        result = {
            "ok": False,
            "torrent_count": 0,
            "paused": False,
            "reannounced": False,
            "deleted": False,
            "error": None,
        }

        try:
            version = self.get_version()
            logger.info(f"[QB] 连接成功，qB 版本: {version}")

            torrents = self.get_all_torrents()
            result["torrent_count"] = len(torrents)
            logger.info(f"[QB] 当前种子数: {len(torrents)}")

            if len(torrents) == 0:
                result["ok"] = True
                result["deleted"] = True
                return result

            result["paused"] = self.pause_all()
            logger.info(f"[QB] 暂停结果: {result['paused']}")

            result["reannounced"] = self.reannounce_all()
            logger.info(f"[QB] Reannounce 结果: {result['reannounced']}")

            wait = cfg.QB_ANNOUNCE_WAIT_SECONDS
            logger.info(f"[QB] 等待 {wait}s 让汇报完成...")
            time.sleep(wait)

            result["deleted"] = self.delete_all(delete_files=True)
            logger.info(f"[QB] 删除结果: {result['deleted']}")

            result["ok"] = result["deleted"]

        except Exception as e:
            result["error"] = str(e)
            logger.error(f"[QB] announce_and_delete_all 异常: {e}")

        return result


# ============================================================
# Vertex 下载器控制
# ============================================================

class VertexDownloaderController:
    """通过 Vertex API 查找并禁用/启用指定 IP 的下载器"""

    def __init__(self, timeout: int = 15):
        self.timeout = timeout

    def _get_headers(self) -> dict:
        cookie_str = get_vertex_manager().get_valid_cookie()
        return {
            "Cookie": cookie_str,
            "Content-Type": "application/json",
        }

    def list_downloaders(self) -> list[dict]:
        url = f"{cfg.VERTEX_LOGIN_URL}/api/downloader/list"
        try:
            r = requests.get(url, headers=self._get_headers(), timeout=self.timeout)
            r.raise_for_status()
            body = r.json()
            if isinstance(body, list):
                return body
            if isinstance(body, dict):
                return body.get("data", body.get("list", []))
            return []
        except Exception as e:
            logger.error(f"[Vertex] 获取下载器列表失败: {e}")
            return []

    def get_proxy_url_for_ip(self, target_ips: list[str]) -> Optional[str]:
        for dl in self.list_downloaders():
            client_url = dl.get("clientUrl", "")
            if not any(ip in client_url for ip in target_ips):
                continue
            dl_id = dl.get("id") or dl.get("_id", "")
            if dl_id:
                return f"{cfg.VERTEX_LOGIN_URL}/proxy/client/{dl_id}/"
        return None

    def _set_downloader_enable(self, downloader: dict, enable: bool) -> bool:
        url = f"{cfg.VERTEX_LOGIN_URL}/api/downloader/modify"
        payload = dict(downloader)
        payload["enable"] = enable
        action_str = "启用" if enable else "禁用"
        try:
            r = requests.post(url, json=payload, headers=self._get_headers(), timeout=self.timeout)
            r.raise_for_status()
            body = r.json()
            ok = (
                body.get("code") == 0
                or body.get("success") is True
                or body.get("msg", "").lower() in ("ok", "success", "")
            )
            if ok:
                logger.info(f"[Vertex] 下载器已{action_str}: {downloader.get('alias')} ({downloader.get('clientUrl')})")
            else:
                logger.warning(f"[Vertex] {action_str}响应异常: {body}")
            return ok
        except Exception as e:
            logger.error(f"[Vertex] {action_str}下载器请求失败: {e}")
            return False

    def disable_downloader(self, downloader: dict) -> bool:
        return self._set_downloader_enable(downloader, False)

    def enable_downloader(self, downloader: dict) -> bool:
        return self._set_downloader_enable(downloader, True)

    def disable_downloaders_by_ips(self, target_ips: list[str]) -> list[dict]:
        if not target_ips:
            return []
        results = []
        for dl in self.list_downloaders():
            client_url = dl.get("clientUrl", "")
            matched_ip = next((ip for ip in target_ips if ip in client_url), None)
            if not matched_ip:
                continue
            alias   = dl.get("alias", dl.get("id", "unknown"))
            enabled = dl.get("enable", False)
            if not enabled:
                logger.info(f"[Vertex] 下载器 [{alias}] 已是禁用状态，跳过")
                results.append({
                    "alias": alias, "clientUrl": client_url,
                    "matched_ip": matched_ip,
                    "action": "already_disabled", "success": True,
                })
                continue
            success = self.disable_downloader(dl)
            results.append({
                "alias": alias, "clientUrl": client_url,
                "matched_ip": matched_ip,
                "action": "disabled" if success else "failed",
                "success": success,
            })
        return results

    def enable_downloaders_by_ips(self, target_ips: list[str]) -> list[dict]:
        if not target_ips:
            return []
        results = []
        for dl in self.list_downloaders():
            client_url = dl.get("clientUrl", "")
            matched_ip = next((ip for ip in target_ips if ip in client_url), None)
            if not matched_ip:
                continue
            alias   = dl.get("alias", dl.get("id", "unknown"))
            enabled = dl.get("enable", False)
            if enabled:
                logger.info(f"[Vertex] 下载器 [{alias}] 已是启用状态，跳过")
                results.append({
                    "alias": alias, "clientUrl": client_url,
                    "matched_ip": matched_ip,
                    "action": "already_enabled", "success": True,
                })
                continue
            success = self.enable_downloader(dl)
            results.append({
                "alias": alias, "clientUrl": client_url,
                "matched_ip": matched_ip,
                "action": "enabled" if success else "failed",
                "success": success,
            })
        return results


vertex_controller = VertexDownloaderController()


# ============================================================
# 阈值处理：qB 汇报删除 + Vertex 禁用
# ============================================================

class ThresholdHandler:

    def handle(self, account_alias: str, info: dict) -> dict:
        """超阈值：qB 清理种子 + Vertex 禁用下载器"""
        target_ips = cfg.account_vertex_ip_map().get(account_alias, [])
        summary = {
            "qb_result": None,
            "vertex_actions": [],
            "qb_proxy_url": None,
        }

        # Step 1: 查找 qB 代理地址
        qb_proxy_url = cfg.account_qb_override().get(account_alias)
        if not qb_proxy_url and target_ips:
            qb_proxy_url = vertex_controller.get_proxy_url_for_ip(target_ips)

        summary["qb_proxy_url"] = qb_proxy_url

        if qb_proxy_url:
            logger.info(f"[Threshold] [{account_alias}] qB 代理地址: {qb_proxy_url}")
            qb = QBittorrentController(proxy_base_url=qb_proxy_url)
            qb_result = qb.announce_and_delete_all()
            summary["qb_result"] = qb_result
            logger.info(f"[Threshold] [{account_alias}] qB 操作结果: {qb_result}")
        else:
            logger.warning(f"[Threshold] [{account_alias}] 未找到 qB 代理地址，跳过种子清理")
            summary["qb_result"] = {"ok": False, "error": "未配置或未找到 qB 代理地址"}

        # Step 2: Vertex 禁用下载器
        if target_ips:
            logger.info(f"[Threshold] [{account_alias}] 开始禁用 Vertex 下载器，目标IP: {target_ips}")
            summary["vertex_actions"] = vertex_controller.disable_downloaders_by_ips(target_ips)
        else:
            logger.info(f"[Threshold] [{account_alias}] 未配置 Vertex IP，跳过禁用操作")

        return summary

    def handle_recovery(self, account_alias: str) -> dict:
        """未超阈值：自动启用之前被禁用的下载器"""
        target_ips = cfg.account_vertex_ip_map().get(account_alias, [])
        summary: dict = {"vertex_actions": [], "actually_acted": []}

        if not target_ips:
            logger.info(f"[Recovery] [{account_alias}] 未配置 Vertex IP，跳过恢复操作")
            return summary

        logger.info(f"[Recovery] [{account_alias}] 检查并恢复 Vertex 下载器，目标IP: {target_ips}")
        vertex_results = vertex_controller.enable_downloaders_by_ips(target_ips)
        summary["vertex_actions"] = vertex_results
        summary["actually_acted"] = [v for v in vertex_results if v["action"] != "already_enabled"]
        return summary


threshold_handler = ThresholdHandler()


# ============================================================
# Cookie 加载
# ============================================================

def load_cookies(account_alias: str) -> dict:
    cookie_map = cfg.account_cookie_map()
    filename = cookie_map.get(account_alias)
    if not filename:
        raise ValueError(f"未找到账号 [{account_alias}] 的 Cookie 映射")
    filepath = os.path.join(cfg.COOKIE_DIR, filename)
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Cookie 文件不存在: {filepath}")
    content = open(filepath, encoding="utf-8").read().strip()
    content = content.removeprefix("Cookie:").strip()
    cookies: dict = {}
    for part in content.replace("\n", ";").split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


# ============================================================
# 工具函数
# ============================================================

def bytes_to_human(b: float) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.2f} {unit}"
        b /= 1024
    return f"{b:.2f} PB"


def get_current_month_range() -> tuple[str, str]:
    now = datetime.now()
    return now.strftime("%Y-%m-01"), now.strftime("%Y-%m-31")


# ============================================================
# LSW VPS Fetcher
# ============================================================

class LswVPSFetcher:

    def fetch(self, account_alias: str, service_id: int) -> dict:
        vps_data = self._post(account_alias, {
            "action": "get_vps_data",
            "service_id": service_id,
        })
        start, end = get_current_month_range()
        metrics_data = self._post(account_alias, {
            "action": "vps_action",
            "service_id": service_id,
            "vps_action": "get_metrics",
            "from": start,
            "to": end,
        })
        ip_status = self._post(account_alias, {
            "action": "vps_action",
            "service_id": service_id,
            "vps_action": "get_ip_status_summary",
        })
        return self._parse(account_alias, service_id, vps_data, metrics_data, ip_status)

    def _post(self, account_alias: str, data: dict) -> dict:
        cookies = load_cookies(account_alias)
        r = requests.post(BASE_API, data=data, cookies=cookies, headers=HEADERS, timeout=30)
        r.raise_for_status()
        return r.json()

    def _parse(self, account_alias, service_id, vps_raw, metrics_raw, ip_raw) -> dict:
        result: dict = {
            "account_alias": account_alias,
            "service_id": service_id,
            "type": "lsw",
            "ok": False,
        }
        try:
            d = vps_raw["data"]
            details = d.get("details", {})
            raw_end = details.get("contract_ends_at", "")
            contract_ends = re.sub(r"<[^>]+>", "", raw_end).strip()
            result.update({
                "ok": True,
                "ip": d.get("ip", "N/A"),
                "ipv6": next((i["ip"] for i in d.get("ips", []) if i["version"] == 6), "N/A"),
                "vcpu": d.get("vcpu", "N/A"),
                "ram": d.get("ram", "N/A"),
                "disk": f"{d.get('disk', 'N/A')} GB",
                "state": d.get("state", "N/A"),
                "os": d.get("os", "N/A"),
                "region": details.get("region", "N/A"),
                "datacenter": details.get("datacenter", "N/A"),
                "network_speed": details.get("network_speed", "N/A"),
                "traffic_limit": details.get("data_traffic", "N/A"),
                "traffic_used": details.get("data_used", "N/A"),
                "contract_ends": contract_ends,
            })
        except Exception as e:
            result["error"] = f"VPS数据解析失败: {e}"
            return result

        try:
            summary    = metrics_raw["data"]["data"]["_metadata"]["summary"]
            down_total = summary["downPublic"]["total"]
            up_total   = summary["upPublic"]["total"]
            down_peak  = summary["downPublic"]["peak"]["value"]
            up_peak    = summary["upPublic"]["peak"]["value"]
            result.update({
                "down_total":      bytes_to_human(down_total),
                "up_total":        bytes_to_human(up_total),
                "total_traffic":   bytes_to_human(down_total + up_total),
                "down_peak":       bytes_to_human(down_peak),
                "up_peak":         bytes_to_human(up_peak),
                "total_bytes":     down_total + up_total,
                "threshold_bytes": up_total,   # LSW 只计出站
            })
        except Exception as e:
            result["error_metrics"] = f"流量数据解析失败: {e}"
            result.update({
                "down_total": "N/A", "up_total": "N/A",
                "total_traffic": "N/A", "down_peak": "N/A",
                "up_peak": "N/A", "total_bytes": 0, "threshold_bytes": 0,
            })

        try:
            ip_d = ip_raw["data"]["data"]
            result["ipv4_null_routed"] = ip_d["ipv4"]["nullRouted"]
            result["ipv6_null_routed"] = ip_d["ipv6"]["nullRouted"]
        except Exception:
            result["ipv4_null_routed"] = None
            result["ipv6_null_routed"] = None

        return result


# ============================================================
# AVM VPS Fetcher
# ============================================================

class AvmVPSFetcher:

    def fetch(self, account_alias: str, service_id: int) -> dict:
        cookies = load_cookies(account_alias)
        r = requests.get(
            AVM_API,
            params={"avmAction": "show", "avmServiceId": service_id},
            cookies=cookies,
            headers=HEADERS,
            timeout=30,
        )
        r.raise_for_status()
        return self._parse(account_alias, service_id, r.json())

    def _parse(self, account_alias: str, service_id: int, raw: dict) -> dict:
        result: dict = {
            "account_alias": account_alias,
            "service_id": service_id,
            "type": "avm",
            "ok": False,
        }
        try:
            d = raw["data"]
            ip      = d.get("reserve", {}).get("address", {}).get("address", "N/A")
            gateway = d.get("reserve", {}).get("address", {}).get("gateway", "N/A")
            netmask = d.get("reserve", {}).get("address", {}).get("netmask", "N/A")
            all_ips = [
                r["address"]["address"]
                for r in d.get("reserves", [])
                if r.get("address", {}).get("address")
            ]
            cpu     = d.get("cpuCore", "N/A")
            ram_mb  = d.get("memorySize", 0)
            ram     = f"{ram_mb / 1024:.0f} GiB" if ram_mb else "N/A"
            disk    = f"{d.get('diskSize', 'N/A')} GB"
            os_name = d.get("template", {}).get("name", "N/A")
            status  = d.get("status", "N/A")
            power   = d.get("powerStatus", {}).get("value", "N/A")
            name    = d.get("name", "N/A")
            datacenter = d.get("section", {}).get("cluster", {}).get("center", {}).get("name", "N/A")
            cluster_ip = d.get("section", {}).get("cluster", {}).get("name", "N/A")

            traffics = sorted(
                d.get("traffics", []),
                key=lambda x: x.get("createdAt", ""),
                reverse=True,
            )
            current_usage_bytes = 0
            current_limit_gb    = 0
            current_since       = "N/A"
            history_rows: list  = []
            for i, t in enumerate(traffics):
                usage_bytes = t.get("trafficUsage", 0) or 0
                limit_gb    = t.get("traffic", 0) or 0
                since       = t.get("createdAt", "N/A")
                t_type      = t.get("type", "N/A")
                t_status    = t.get("status", "N/A")
                if i == 0:
                    current_usage_bytes = usage_bytes
                    current_limit_gb    = limit_gb
                    current_since       = since
                else:
                    history_rows.append({
                        "since": since, "usage": bytes_to_human(usage_bytes),
                        "limit": f"{limit_gb} GB", "type": t_type, "status": t_status,
                    })

            result.update({
                "ok": True,
                "ip": ip, "all_ips": all_ips, "gateway": gateway, "netmask": netmask,
                "vcpu": cpu, "ram": ram, "disk": disk, "os": os_name,
                "status": status, "power": power, "name": name,
                "datacenter": datacenter, "cluster_ip": cluster_ip,
                "traffic_used":    bytes_to_human(current_usage_bytes),
                "traffic_limit":   f"{current_limit_gb} GB",
                "traffic_since":   current_since,
                "total_bytes":     current_usage_bytes,
                "threshold_bytes": current_usage_bytes,   # AVM 双向
                "threshold_exceeded": False,
                "traffic_history": history_rows,
            })
        except Exception as e:
            result["error"] = f"AVM数据解析失败: {e}"
        return result


lsw_fetcher = LswVPSFetcher()
avm_fetcher = AvmVPSFetcher()


# ============================================================
# 核心检查（每次调用都从 cfg 读取最新配置）
# ============================================================

def check_all() -> list[dict]:
    # 每轮从 cfg 热重载最新配置
    account_products   = cfg.account_products()
    thresholds_tb      = cfg.traffic_thresholds_tb()
    global_warn_tb     = cfg.TRAFFIC_THRESHOLD_TB

    results = []
    for account_alias, products in account_products.items():
        for product in products:
            pid   = product["id"]
            ptype = product["type"]
            try:
                if ptype == "lsw":
                    info = lsw_fetcher.fetch(account_alias, pid)
                elif ptype == "avm":
                    info = avm_fetcher.fetch(account_alias, pid)
                else:
                    raise ValueError(f"未知机器类型: {ptype}")

                threshold_tb      = thresholds_tb.get(account_alias, global_warn_tb)
                threshold_bytes   = info.get("threshold_bytes", info.get("total_bytes", 0))
                threshold_tb_used = threshold_bytes / (1024 ** 4)

                info["threshold_tb"]       = threshold_tb
                info["threshold_tb_used"]  = round(threshold_tb_used, 3)
                info["threshold_exceeded"] = threshold_tb_used >= threshold_tb

                total_tb = info.get("total_bytes", 0) / (1024 ** 4)
                info["warn_exceeded"] = total_tb >= global_warn_tb

                info["threshold_summary"] = {}
                info["recovery_summary"]  = {}

                if info["threshold_exceeded"]:
                    summary = threshold_handler.handle(account_alias, info)
                    info["threshold_summary"] = summary
                    info["vertex_actions"]    = summary.get("vertex_actions", [])
                else:
                    recovery = threshold_handler.handle_recovery(account_alias)
                    info["recovery_summary"] = recovery
                    info["vertex_actions"]   = recovery.get("vertex_actions", [])

                results.append(info)

            except Exception as e:
                results.append({
                    "account_alias": account_alias,
                    "service_id": pid,
                    "type": ptype,
                    "ok": False,
                    "error": str(e),
                })
    return results


# ============================================================
# IP 遮罩 / 转义工具
# ============================================================

def mask_ip_partial(ip: str) -> str:
    parts = ip.split(".")
    if len(parts) == 4:
        return f"`{parts[0]}.{parts[1]}.*.*`"
    parts6 = ip.split(":")
    if len(parts6) >= 2:
        return f"`{parts6[0]}:{parts6[1]}:*:*:*:*:*:*`"
    return f"`{ip}`"


def tg_escape(text: str) -> str:
    for ch in r'_*[]()~`>#+-=|{}.!':
        text = str(text).replace(ch, f"\\{ch}")
    return text


# ============================================================
# 流量进度条
# ============================================================

def traffic_bar(used_tb: float, limit_tb: float, width: int = 10) -> str:
    if limit_tb <= 0:
        return ""
    pct = min(used_tb / limit_tb, 1.0)
    filled = round(pct * width)
    bar = "█" * filled + "░" * (width - filled)
    pct_str = tg_escape(f"{pct*100:.1f}")
    return f"\\[{bar}\\] {pct_str}%"


# ============================================================
# qB 操作结果通知段落（MarkdownV2）
# ============================================================

def fmt_qb_section(qb_result: dict | None, proxy_url: str | None) -> str:
    if qb_result is None:
        return ""

    lines = ["\n🧹 *qBittorrent 清理操作*:"]

    if proxy_url:
        proxy_display = re.sub(r"http://[^/]+", "http://***", proxy_url)
        lines.append(f"  🔗 代理: `{tg_escape(proxy_display)}`")

    if not qb_result.get("ok"):
        err = tg_escape(str(qb_result.get("error", "未知错误")))
        lines.append(f"  ❌ 操作失败: {err}")
        return "\n".join(lines)

    torrent_count = qb_result.get("torrent_count", 0)
    paused        = qb_result.get("paused", False)
    reannounced   = qb_result.get("reannounced", False)
    deleted       = qb_result.get("deleted", False)

    lines.append(f"  📦 种子数量: `{torrent_count}` 个")
    lines.append(f"  ⏸️ 暂停所有: {'✅' if paused else '❌'}")
    lines.append(f"  📡 向 Tracker 汇报: {'✅' if reannounced else '❌'}")
    lines.append(f"  🗑️ 删除含文件: {'✅' if deleted else '❌'}")
    if deleted:
        lines.append("  ✅ *全部种子已清理完毕*")

    return "\n".join(lines)


# ============================================================
# 格式化输出（控制台）
# ============================================================

def fmt_console(info: dict) -> str:
    if not info.get("ok"):
        return (f"❌ 账号: {info['account_alias']}  ID: {info['service_id']}  类型: {info.get('type','?')}\n"
                f"   错误: {info.get('error')}")

    warn = "🚨 [超阈值警告]" if info.get("threshold_exceeded") else "✅"

    qb_console = ""
    summary = info.get("threshold_summary", {})
    if summary:
        qb_r = summary.get("qb_result", {})
        if qb_r:
            status = "成功" if qb_r.get("ok") else f"失败({qb_r.get('error','')})"
            qb_console = (
                f"\n   ─────────────────────\n"
                f"   [qB清理] 种子数:{qb_r.get('torrent_count',0)} | "
                f"暂停:{qb_r.get('paused')} | 汇报:{qb_r.get('reannounced')} | "
                f"删除:{qb_r.get('deleted')} | {status}\n"
            )

    if info["type"] == "lsw":
        ipv4_s = "🔴 已被 Null Route" if info.get("ipv4_null_routed") else "🟢 正常"
        ipv6_s = "🔴 已被 Null Route" if info.get("ipv6_null_routed") else "🟢 正常"
        return (
            f"{warn} [LSW] 账号:       {info['account_alias']}\n"
            f"   服务ID:       {info['service_id']}\n"
            f"   状态:         {info['state']}\n"
            f"   系统:         {info['os']}\n"
            f"   IPv4:         {info['ip']} [{ipv4_s}]\n"
            f"   IPv6:         {info['ipv6']} [{ipv6_s}]\n"
            f"   CPU/RAM:      {info['vcpu']} vCPU / {info['ram']}\n"
            f"   硬盘:         {info['disk']}\n"
            f"   数据中心:     {info['datacenter']} ({info['region']})\n"
            f"   网络速度:     {info['network_speed']}\n"
            f"   流量限额:     {info['traffic_limit']} (出站 {info['threshold_tb']} TB 触发)\n"
            f"   已用流量:     {info['traffic_used']}\n"
            f"   ─────────────────────\n"
            f"   本月下行:     {info['down_total']}\n"
            f"   本月上行:     {info['up_total']}\n"
            f"   本月合计:     {info['total_traffic']}\n"
            f"   峰值下行:     {info['down_peak']}\n"
            f"   峰值上行:     {info['up_peak']}\n"
            f"   合同到期:     {info['contract_ends']}\n"
            + qb_console
        )

    elif info["type"] == "avm":
        history_str = ""
        for h in info.get("traffic_history", []):
            history_str += f"     [{h['since']}] 用量:{h['usage']} / 限额:{h['limit']} ({h['type']})\n"
        return (
            f"{warn} [AVM] 账号:       {info['account_alias']}\n"
            f"   服务ID:       {info['service_id']}\n"
            f"   机器名:       {info['name']}\n"
            f"   电源状态:     {info['power']}\n"
            f"   服务状态:     {info['status']}\n"
            f"   系统:         {info['os']}\n"
            f"   IP:           {', '.join(info['all_ips'])}\n"
            f"   网关:         {info['gateway']}  掩码:{info['netmask']}\n"
            f"   CPU/RAM:      {info['vcpu']} vCPU / {info['ram']}\n"
            f"   硬盘:         {info['disk']}\n"
            f"   数据中心:     {info['datacenter']} ({info['cluster_ip']})\n"
            f"   ─────────────────────\n"
            f"   当前周期始:   {info['traffic_since']}\n"
            f"   已用流量:     {info['traffic_used']}\n"
            f"   流量限额:     {info['traffic_limit']} (双向 {info['threshold_tb']} TB 触发)\n"
            + (f"   历史流量:\n{history_str}" if history_str else "")
            + qb_console
        )

    return str(info)


# ============================================================
# 格式化输出（Telegram MarkdownV2）
# ============================================================

def fmt_telegram(info: dict) -> str:
    if not info.get("ok"):
        return (
            f"❌ *{tg_escape(info['account_alias'])}* \\| ID `{info['service_id']}`\n"
            f"错误: {tg_escape(str(info.get('error', '')))}"
        )

    threshold_exceeded = info.get("threshold_exceeded", False)
    warn_exceeded      = info.get("warn_exceeded", False)

    if threshold_exceeded:
        status_icon = "🚨"
        status_line = "🚨 *\\[超阈值\\！种子已清理，下载器已禁用\\]* 🚨\n"
    elif warn_exceeded:
        status_icon = "⚠️"
        status_line = f"⚠️ *\\[流量告警：已超 {tg_escape(str(cfg.TRAFFIC_THRESHOLD_TB))} TB\\]*\n"
    else:
        status_icon = "✅"
        status_line = ""

    # ── qB 操作段落 ───────────────────────────────────────────
    qb_section = ""
    threshold_summary = info.get("threshold_summary", {})
    if threshold_summary:
        qb_section = fmt_qb_section(
            threshold_summary.get("qb_result"),
            threshold_summary.get("qb_proxy_url"),
        )

    # ── Vertex 操作段落 ──────────────────────────────────────
    vertex_section = ""
    vertex_actions = info.get("vertex_actions", [])
    if vertex_actions:
        lines = []
        for va in vertex_actions:
            action_map = {
                "disabled":         ("🔴", "已禁用"),
                "already_disabled": ("⚪", "已是禁用"),
                "enabled":          ("🟢", "已启用"),
                "already_enabled":  ("⚪", "已是启用"),
                "failed":           ("❗", "操作失败"),
            }
            icon, action_str = action_map.get(va["action"], ("❓", va["action"]))
            alias_esc = tg_escape(va["alias"])
            lines.append(f"  {icon} `{alias_esc}` → {tg_escape(action_str)}")

        has_real_action = any(
            va["action"] not in ("already_enabled", "already_disabled")
            for va in vertex_actions
        )
        if has_real_action:
            vertex_section = "\n🖥️ *Vertex 下载器操作*:\n" + "\n".join(lines)

    # ── LSW ──────────────────────────────────────────────────
    if info["type"] == "lsw":
        ipv4_s = "🔴 Null Routed" if info.get("ipv4_null_routed") else "🟢 正常"
        ipv6_s = "🔴 Null Routed" if info.get("ipv6_null_routed") else "🟢 正常"
        ipv4_masked = mask_ip_partial(info["ip"])
        ipv6_masked = mask_ip_partial(info["ipv6"])
        used_tb_display = info.get("threshold_tb_used", 0)
        limit_tb        = info.get("threshold_tb", 25.0)
        bar = traffic_bar(used_tb_display, limit_tb)

        return (
            f"{status_line}"
            f"{status_icon} 📦 *\\[LSW\\] {tg_escape(info['account_alias'])}*\n"
            f"🆔 *服务 ID*: `{info['service_id']}`\n"
            f"🖥️ *状态*: `{tg_escape(info['state'])}` / `{tg_escape(info['os'])}`\n"
            f"🌐 *IPv4*: {ipv4_masked} {ipv4_s}\n"
            f"🌐 *IPv6*: {ipv6_masked} {ipv6_s}\n"
            f"⚙️ *配置*: `{info['vcpu']}` vCPU / `{tg_escape(info['ram'])}` / `{info['disk']}`\n"
            f"📍 *数据中心*: `{tg_escape(info['datacenter'])}` \\(`{tg_escape(info['region'])}`\\)\n"
            f"🚀 *网速*: `{tg_escape(info['network_speed'])}`\n"
            f"\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\n"
            f"📊 *出站阈值*: `{tg_escape(str(used_tb_display))} TB` / `{tg_escape(str(limit_tb))} TB`\n"
            f"📉 {bar}\n"
            f"📈 *套餐流量*: `{tg_escape(info['traffic_limit'])}` \\| 已用 `{tg_escape(info['traffic_used'])}`\n"
            f"⬇️ *本月下行*: `{tg_escape(info['down_total'])}`\n"
            f"⬆️ *本月上行*: `{tg_escape(info['up_total'])}`\n"
            f"💾 *本月合计*: `{tg_escape(info['total_traffic'])}`\n"
            f"📉 *峰值下行*: `{tg_escape(info['down_peak'])}`\n"
            f"📉 *峰值上行*: `{tg_escape(info['up_peak'])}`\n"
            f"📅 *合同到期*: `{tg_escape(info['contract_ends'])}`"
            f"{qb_section}"
            f"{vertex_section}"
        )

    # ── AVM ──────────────────────────────────────────────────
    elif info["type"] == "avm":
        ips_masked = " / ".join(mask_ip_partial(ip) for ip in info["all_ips"])
        gw_masked  = mask_ip_partial(info["gateway"])
        history_lines = ""
        for h in info.get("traffic_history", []):
            history_lines += (
                f"  • `{tg_escape(h['since'])}` "
                f"用量:`{tg_escape(h['usage'])}` / "
                f"限额:`{tg_escape(h['limit'])}`\n"
            )
        used_tb_display = info.get("threshold_tb_used", 0)
        limit_tb        = info.get("threshold_tb", 50.0)
        bar = traffic_bar(used_tb_display, limit_tb)

        return (
            f"{status_line}"
            f"{status_icon} 📦 *\\[AVM\\] {tg_escape(info['account_alias'])}*\n"
            f"🆔 *服务 ID*: `{info['service_id']}`\n"
            f"🏷️ *机器名*: `{tg_escape(info['name'])}`\n"
            f"🖥️ *系统*: `{tg_escape(info['os'])}`\n"
            f"⚡ *电源*: `{tg_escape(info['power'])}` / *服务*: `{tg_escape(info['status'])}`\n"
            f"🌐 *IP*: {ips_masked}\n"
            f"🔀 *网关*: {gw_masked} 掩码: `{tg_escape(info['netmask'])}`\n"
            f"⚙️ *配置*: `{info['vcpu']}` vCPU / `{tg_escape(info['ram'])}` / `{info['disk']}`\n"
            f"📍 *数据中心*: `{tg_escape(info['datacenter'])}` \\(`{tg_escape(info['cluster_ip'])}`\\)\n"
            f"\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\n"
            f"📊 *双向阈值*: `{tg_escape(str(used_tb_display))} TB` / `{tg_escape(str(limit_tb))} TB`\n"
            f"📉 {bar}\n"
            f"🕐 *当前周期起始*: `{tg_escape(info['traffic_since'])}`\n"
            f"📈 *已用流量*: `{tg_escape(info['traffic_used'])}`\n"
            f"📊 *套餐限额*: `{tg_escape(info['traffic_limit'])}`\n"
            + (f"📋 *历史流量*:\n{history_lines}" if history_lines else "")
            + qb_section
            + vertex_section
        )

    return tg_escape(str(info))


# ============================================================
# 定时推送主循环
# ============================================================

async def push_loop():
    print("✅ 定时推送启动（配置文件支持热重载）")

    while True:
        # 每轮从 cfg 读取最新配置（自动热重载）
        interval = cfg.PUSH_INTERVAL
        chat_ids = cfg.TARGET_CHAT_IDS
        bot      = Bot(token=cfg.TG_BOT_TOKEN)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n{'='*55}")
        print(f"[{now}] 开始查询所有账号  (推送间隔: {interval // 60} 分钟)")
        print(f"{'='*55}")

        results = check_all()

        for info in results:
            print(f"\n{'─'*50}")
            print(fmt_console(info))
            print(f"{'─'*50}")

            msg = fmt_telegram(info)
            for chat_id in chat_ids:
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=msg,
                        parse_mode="MarkdownV2",
                    )
                    print(f"[TG推送成功] chat_id={chat_id}")
                except Exception as e:
                    print(f"[TG推送失败] chat_id={chat_id}: {e}")

        print(f"\n[等待] {interval // 60} 分钟后再次推送...")
        await asyncio.sleep(interval)


# ============================================================
# 入口
# ============================================================

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    os.makedirs(cfg.COOKIE_DIR, exist_ok=True)
    asyncio.run(push_loop())


if __name__ == "__main__":
    main()
