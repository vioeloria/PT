"""
vertex修改删种.py — Vertex 批量修改工具（集成 Cookie 自动刷新）
=============================================================
配置方式（三选一，优先级从高到低）：

1. config.yaml（推荐）:
   vertex:
     url: ""
     username: "admin"
     password: "your_plaintext_password"   # 明文，会自动 MD5
     # 或
     # password_md5: "xxxxx32位哈希xxxxx"  # 直接给 MD5

2. 环境变量:
   export VTURL=""
   export VT_USERNAME="admin"
   export VT_PASSWORD="your_plaintext_password"

3. 回退：若以上均未配置，程序启动时交互式询问
"""

from __future__ import annotations

import os
import sys
import logging
from typing import Optional

import requests

# ── 尝试导入 vertex_cookie ──────────────────────────────────────────
try:
    from vertex_cookie import VertexCookieManager, from_env
    _COOKIE_MODULE_AVAILABLE = True
except ImportError:
    _COOKIE_MODULE_AVAILABLE = False
    print("⚠  未找到 vertex_cookie.py，将回退到 cookies.txt 模式")

# ── 尝试导入 PyYAML ────────────────────────────────────────────────
try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════
# 配置加载
# ══════════════════════════════════════════════════════════════════

def _load_config_yaml(path: str = "config.yaml") -> dict:
    """读取 config.yaml，不存在或解析失败则返回空 dict"""
    if not _YAML_AVAILABLE:
        return {}
    # 优先查当前目录，其次查脚本所在目录
    candidates = [path, os.path.join(os.path.dirname(os.path.abspath(__file__)), path)]
    for p in candidates:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                    return data.get("vertex", data)  # 支持顶层或 vertex: 节点
            except Exception as e:
                print(f"⚠  读取 config.yaml 失败: {e}")
    return {}


def build_cookie_manager() -> Optional["VertexCookieManager"]:
    """
    按优先级构建 VertexCookieManager：
      config.yaml > 环境变量 > 交互式输入
    返回 None 表示构建失败（程序将退出）。
    """
    if not _COOKIE_MODULE_AVAILABLE:
        return None

    cfg = _load_config_yaml()

    # 1. config.yaml
    url      = cfg.get("url", "").rstrip("/")
    username = cfg.get("username", "")
    password = cfg.get("password", "")          # 明文
    pwd_md5  = cfg.get("password_md5", "")      # MD5

    if url and username and (password or pwd_md5):
        raw = password or pwd_md5
        is_hashed = bool(pwd_md5 and not password)
        print(f"✓ 使用 config.yaml 配置: {url} / {username}")
        return VertexCookieManager(url, username, raw, password_is_hashed=is_hashed)

    # 2. 环境变量
    vcm = from_env()
    if vcm:
        print(f"✓ 使用环境变量配置: {vcm.login_url} / {vcm.username}")
        return vcm

    # 3. 交互式输入
    print("\n未找到配置，请手动输入 Vertex 连接信息:")
    url      = input("Vertex URL (例: http://23.82.99.203:3077): ").strip().rstrip("/")
    username = input("用户名: ").strip()
    password = input("密码（明文，将自动 MD5）: ").strip()

    if not (url and username and password):
        print("❌ 信息不完整，程序退出")
        sys.exit(1)

    return VertexCookieManager(url, username, password, password_is_hashed=False)


# ══════════════════════════════════════════════════════════════════
# Cookie 工具：统一从 manager 或 cookies.txt 获取 Header/Cookie dict
# ══════════════════════════════════════════════════════════════════

class CookieProvider:
    """
    统一的 Cookie 提供者。
    · 优先使用 VertexCookieManager（自动刷新）
    · 回退到 cookies.txt 文件读取
    """

    def __init__(self, manager: Optional["VertexCookieManager"] = None,
                 cookies_file: str = "cookies.txt"):
        self._manager     = manager
        self._cookies_file = cookies_file
        self._fallback_cookies: dict = {}

        if manager is None:
            self._fallback_cookies = self._load_file_cookies()

    def _load_file_cookies(self) -> dict:
        cookies_dict = {}
        try:
            with open(self._cookies_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and "=" in line:
                        k, v = line.split("=", 1)
                        cookies_dict[k.strip()] = v.strip()
        except FileNotFoundError:
            print(f"❌ 找不到 Cookie 文件: {self._cookies_file}")
            sys.exit(1)
        return cookies_dict

    def get_headers(self) -> dict:
        """返回包含 Cookie 的请求头字典"""
        if self._manager:
            cookie_str = self._manager.get_valid_cookie()
            return {"Cookie": cookie_str, "Content-Type": "application/json"}
        return {"Content-Type": "application/json"}

    def get_cookies(self) -> dict:
        """返回 cookies dict（用于 requests 的 cookies= 参数）"""
        if self._manager:
            return {}   # 使用 headers= 方式，不需要 cookies=
        return self._fallback_cookies

    def apply(self, req_kwargs: dict) -> dict:
        """
        将 cookie 信息注入 requests 调用参数。
        使用 manager 时走 headers Cookie，否则走 cookies dict。
        """
        if self._manager:
            headers = req_kwargs.setdefault("headers", {})
            headers.update(self.get_headers())
        else:
            req_kwargs["cookies"] = self.get_cookies()
        return req_kwargs


# ══════════════════════════════════════════════════════════════════
# VertexModifier  （下载器批量修改）
# ══════════════════════════════════════════════════════════════════

class VertexModifier:
    def __init__(self, cookie_provider: CookieProvider,
                 base_url: str, filter_keyword: str = ""):
        self.cp             = cookie_provider
        self.base_url       = base_url.rstrip("/") + "/api/downloader"
        self.delete_rule_url = base_url.rstrip("/") + "/api/deleteRule"
        self.filter_keyword = filter_keyword

    # ── API 请求封装 ──────────────────────────────────────────────

    def _get(self, url: str) -> Optional[dict]:
        try:
            kw = self.cp.apply({"timeout": 15})
            r  = requests.get(url, **kw)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            print(f"请求失败 [{url}]: {e}")
            return None

    def _post(self, url: str, payload: dict) -> Optional[dict]:
        try:
            kw = self.cp.apply({"json": payload, "timeout": 15})
            r  = requests.post(url, **kw)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            print(f"请求失败 [{url}]: {e}")
            return None

    # ── 业务方法 ──────────────────────────────────────────────────

    def get_downloader_list(self) -> Optional[dict]:
        return self._get(f"{self.base_url}/list")

    def get_delete_rules(self) -> dict:
        data = self._get(f"{self.delete_rule_url}/list")
        if data and data.get("success"):
            return {rule["id"]: rule.get("name", rule["id"]) for rule in data.get("data", [])}
        return {}

    def display_rules_summary(self, filtered_clients: list):
        print("\n" + "=" * 60)
        print("📋 当前删种规则汇总（可复制规则ID用于批量设置）")
        print("=" * 60)

        rule_name_map       = self.get_delete_rules()
        all_rule_ids_ordered = []
        seen_ids            = set()

        for client in filtered_clients:
            rules        = client.get("deleteRules", [])
            client_alias = client.get("alias", client.get("id", "未知"))
            print(f"\n  📦 {client_alias}")
            if not rules:
                print("     （无删种规则）")
            else:
                print(f"     规则数: {len(rules)} 条")
                for rid in rules:
                    name         = rule_name_map.get(rid, "")
                    display_name = f"  ← {name}" if name else ""
                    print(f"       • {rid}{display_name}")
                    if rid not in seen_ids:
                        seen_ids.add(rid)
                        all_rule_ids_ordered.append(rid)

        print("\n" + "-" * 60)
        print("🔑 所有规则去重汇总（按首次出现顺序）:")
        if all_rule_ids_ordered:
            for rid in all_rule_ids_ordered:
                name         = rule_name_map.get(rid, "")
                display_name = f"  ← {name}" if name else ""
                print(f"   • {rid}{display_name}")
            print("\n📋 可直接复制的格式（用于批量设置删种规则）:")
            print(f"   {','.join(all_rule_ids_ordered)}")
        else:
            print("   （所有下载器均无删种规则）")
        print("=" * 60)

    def filter_clients(self, data: dict) -> list:
        if not data or not data.get("success"):
            return []
        return [c for c in data.get("data", []) if self.filter_keyword in c.get("alias", "")]

    def modify_client(self, client: dict, new_rules=None, max_leech_num=None,
                      auto_delete_cron=None, min_free_space=None,
                      min_free_space_unit=None, max_upload_speed=None,
                      max_upload_speed_unit=None) -> Optional[dict]:
        payload = client.copy()
        if new_rules           is not None: payload["deleteRules"]      = new_rules
        if max_leech_num       is not None: payload["maxLeechNum"]       = max_leech_num
        if auto_delete_cron    is not None: payload["autoDeleteCron"]    = auto_delete_cron
        if min_free_space      is not None: payload["minFreeSpace"]      = min_free_space
        if min_free_space_unit is not None: payload["minFreeSpaceUnit"]  = min_free_space_unit
        if max_upload_speed    is not None: payload["maxUploadSpeed"]    = max_upload_speed
        if max_upload_speed_unit is not None: payload["maxUploadSpeedUnit"] = max_upload_speed_unit
        return self._post(f"{self.base_url}/modify", payload)

    def run(self):
        print("=" * 60)
        print("Vertex Downloader 批量修改工具")
        print("=" * 60)

        print("\n[1/9] 正在获取下载器列表...")
        data = self.get_downloader_list()
        if not data:
            print("❌ 获取列表失败")
            return

        if not self.filter_keyword:
            print("\n[2/9] 请输入要筛选的alias关键字:")
            print("示例: Netcup, Hetzner, 或其他关键字")
            self.filter_keyword = input(">>> ").strip()
            if not self.filter_keyword:
                print("❌ 未输入关键字，操作取消")
                return
        else:
            print(f"\n[2/9] 使用预设关键字: {self.filter_keyword}")

        print(f"[3/9] 正在筛选包含'{self.filter_keyword}'的客户端...")
        filtered_clients = self.filter_clients(data)
        if not filtered_clients:
            print(f"❌ 未找到包含'{self.filter_keyword}'的客户端")
            return

        print(f"✓ 找到 {len(filtered_clients)} 个匹配的客户端:")
        for i, client in enumerate(filtered_clients, 1):
            print(f"  {i}. {client['alias']} (ID: {client['id']})")
            print(f"     当前规则数: {len(client.get('deleteRules', []))}")
            print(f"     当前最大下载数: {client.get('maxLeechNum', '未设置')}")
            print(f"     当前删种间隔: {client.get('autoDeleteCron', '未设置')}")
            print(f"     当前最小剩余空间: {client.get('minFreeSpace', '未设置')} {client.get('minFreeSpaceUnit', 'GiB')}")
            print(f"     当前上传速度上限: {client.get('maxUploadSpeed', '未设置')} {client.get('maxUploadSpeedUnit', 'MiB')}")

        self.display_rules_summary(filtered_clients)

        # ── 各项修改询问 ───────────────────────────────────────
        print("\n[4/9] 是否需要修改删种规则? (y/n)")
        modify_rules = input(">>> ").strip().lower() == "y"
        new_rules = None
        if modify_rules:
            print("\n请输入新的删种规则ID（用逗号分隔）:")
            user_input = input(">>> ").strip()
            if user_input:
                new_rules = [r.strip() for r in user_input.split(",") if r.strip()]
                print(f"解析到 {len(new_rules)} 条规则: {new_rules}")
            else:
                modify_rules = False

        print("\n[5/9] 是否需要修改最大同时下载数? (y/n)")
        modify_max_leech = input(">>> ").strip().lower() == "y"
        max_leech_num = None
        if modify_max_leech:
            user_input = input("请输入新的最大同时下载数（留空=不限制）: ").strip()
            max_leech_num = int(user_input) if user_input.isdigit() else ""

        print("\n[6/9] 是否需要修改删种间隔? (y/n)")
        modify_cron = input(">>> ").strip().lower() == "y"
        auto_delete_cron = None
        if modify_cron:
            cron_options = {
                "1": "*/15 * * * * *", "2": "*/30 * * * * *",
                "3": "0 */1 * * * *",  "4": "0 */5 * * * *",
                "5": "0 */10 * * * *",
            }
            print("1.每15秒  2.每30秒  3.每1分钟  4.每5分钟  5.每10分钟  6.自定义")
            choice = input("请输入选项(1-6): ").strip()
            if choice in cron_options:
                auto_delete_cron = cron_options[choice]
            elif choice == "6":
                auto_delete_cron = input("请输入自定义cron表达式: ").strip() or None
            if not auto_delete_cron:
                modify_cron = False

        print("\n[7/9] 是否需要修改最小剩余空间? (y/n)")
        modify_min_space = input(">>> ").strip().lower() == "y"
        min_free_space = min_free_space_unit = None
        if modify_min_space:
            user_input = input("请输入大小（数字）: ").strip()
            if user_input.isdigit():
                min_free_space = str(int(user_input))
                unit_choice    = input("单位: 1.GiB 2.MiB 3.TiB (默认1): ").strip()
                min_free_space_unit = {"1": "GiB", "2": "MiB", "3": "TiB", "": "GiB"}.get(unit_choice, "GiB")
            else:
                modify_min_space = False

        print("\n[8/9] 是否需要修改上传速度上限? (y/n)")
        modify_max_upload = input(">>> ").strip().lower() == "y"
        max_upload_speed = max_upload_speed_unit = None
        if modify_max_upload:
            user_input = input("请输入速度上限（数字，留空=不限制）: ").strip()
            if user_input.isdigit():
                max_upload_speed = str(int(user_input))
                unit_choice      = input("单位: 1.MiB 2.KiB 3.GiB (默认1): ").strip()
                max_upload_speed_unit = {"1": "MiB", "2": "KiB", "3": "GiB", "": "MiB"}.get(unit_choice, "MiB")
            else:
                max_upload_speed = ""
                max_upload_speed_unit = "MiB"

        if not any([modify_rules, modify_max_leech, modify_cron, modify_min_space, modify_max_upload]):
            print("\n❌ 未选择任何修改项，操作取消")
            return

        print(f"\n[9/9] 即将修改 {len(filtered_clients)} 个客户端")
        if modify_rules:      print(f"  - 删种规则: {new_rules}")
        if modify_max_leech:  print(f"  - 最大下载数: {max_leech_num if max_leech_num != '' else '不限制'}")
        if modify_cron:       print(f"  - 删种间隔: {auto_delete_cron}")
        if modify_min_space:  print(f"  - 最小剩余空间: {min_free_space} {min_free_space_unit}")
        if modify_max_upload: print(f"  - 上传速度上限: {max_upload_speed or '不限制'} {max_upload_speed_unit or ''}")

        if input("\n确认继续? (y/n): ").strip().lower() != "y":
            print("❌ 操作已取消")
            return

        success_count = fail_count = 0
        for client in filtered_clients:
            print(f"\n正在修改: {client['alias']}")
            result = self.modify_client(
                client,
                new_rules=new_rules if modify_rules else None,
                max_leech_num=max_leech_num if modify_max_leech else None,
                auto_delete_cron=auto_delete_cron if modify_cron else None,
                min_free_space=min_free_space if modify_min_space else None,
                min_free_space_unit=min_free_space_unit if modify_min_space else None,
                max_upload_speed=max_upload_speed if modify_max_upload else None,
                max_upload_speed_unit=max_upload_speed_unit if modify_max_upload else None,
            )
            if result and result.get("success"):
                print("  ✓ 修改成功"); success_count += 1
            else:
                print("  ✗ 修改失败"); fail_count += 1

        print("\n" + "=" * 60)
        print(f"修改完成！  成功: {success_count}  失败: {fail_count}")
        print("=" * 60)


# ══════════════════════════════════════════════════════════════════
# RSSModifier  （RSS任务批量修改）
# ══════════════════════════════════════════════════════════════════

class RSSModifier:
    def __init__(self, cookie_provider: CookieProvider,
                 base_url: str, filter_keyword: str = ""):
        self.cp             = cookie_provider
        self.base_url       = base_url.rstrip("/") + "/api/rss"
        self.filter_keyword = filter_keyword

    def _get(self, url: str) -> Optional[dict]:
        try:
            kw = self.cp.apply({"timeout": 15})
            r  = requests.get(url, **kw)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            print(f"请求失败 [{url}]: {e}")
            return None

    def _post(self, url: str, payload: dict) -> Optional[dict]:
        try:
            kw = self.cp.apply({"json": payload, "timeout": 15})
            r  = requests.post(url, **kw)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            print(f"请求失败 [{url}]: {e}")
            return None

    def get_rss_list(self) -> Optional[dict]:
        return self._get(f"{self.base_url}/list")

    def filter_rss_tasks(self, data: dict) -> list:
        if not data or not data.get("success"):
            return []
        return [t for t in data.get("data", []) if self.filter_keyword in t.get("alias", "")]

    def modify_rss_task(self, task: dict, client_sort_by=None,
                        max_client_download_count=None, skip_same_torrent=None,
                        cron=None, client_arr=None) -> Optional[dict]:
        payload = task.copy()
        if client_sort_by             is not None: payload["clientSortBy"]            = client_sort_by
        if max_client_download_count  is not None: payload["maxClientDownloadCount"]  = max_client_download_count
        if skip_same_torrent          is not None: payload["skipSameTorrent"]         = skip_same_torrent
        if cron                       is not None: payload["cron"]                    = cron
        if client_arr                 is not None: payload["clientArr"]               = client_arr
        return self._post(f"{self.base_url}/modify", payload)

    def run(self):
        print("=" * 60)
        print("Vertex RSS任务 批量修改工具")
        print("=" * 60)

        print("\n[1/8] 正在获取RSS任务列表...")
        data = self.get_rss_list()
        if not data:
            print("❌ 获取RSS列表失败")
            return

        if not self.filter_keyword:
            print("\n[2/8] 请输入要筛选的alias关键字（留空=修改所有）:")
            self.filter_keyword = input(">>> ").strip()

        filtered_tasks = self.filter_rss_tasks(data) if self.filter_keyword else data.get("data", [])
        if not filtered_tasks:
            print("❌ 未找到匹配的RSS任务")
            return

        print(f"✓ 找到 {len(filtered_tasks)} 个RSS任务:")
        for i, task in enumerate(filtered_tasks, 1):
            client_arr = task.get("clientArr", [])
            print(f"  {i}. {task['alias']} (ID: {task['id']})")
            print(f"     当前排序方式: {task.get('clientSortBy', '未设置')}")
            print(f"     当前下载器任务上限: {task.get('maxClientDownloadCount', '未设置')}")
            print(f"     当前跳过相同种子: {task.get('skipSameTorrent', False)}")
            print(f"     当前抓取间隔: {task.get('cron', '未设置')}")
            print(f"     当前下载器列表: [{', '.join(client_arr)}]" if client_arr else "     当前下载器列表: （空）")

        # ── 各项修改询问 ───────────────────────────────────────
        print("\n[4/8] 是否需要修改客户端排序规则? (y/n)")
        modify_sort = input(">>> ").strip().lower() == "y"
        client_sort_by = None
        if modify_sort:
            print("1. uploadSpeed  2. leechingCount")
            choice = input("请输入选项(1-2): ").strip()
            client_sort_by = {"1": "uploadSpeed", "2": "leechingCount"}.get(choice)
            if not client_sort_by:
                modify_sort = False

        print("\n[5/8] 是否需要修改下载器任务上限? (y/n)")
        modify_max_download = input(">>> ").strip().lower() == "y"
        max_client_download_count = None
        if modify_max_download:
            user_input = input("请输入新的下载器任务上限（留空=不限制）: ").strip()
            max_client_download_count = str(int(user_input)) if user_input.isdigit() else ""

        print("\n[6/8] 是否需要修改跳过相同种子设置? (y/n)")
        modify_skip_same = input(">>> ").strip().lower() == "y"
        skip_same_torrent = None
        if modify_skip_same:
            print("1. 启用 (true)  2. 禁用 (false)")
            choice = input("请输入选项(1-2): ").strip()
            if choice == "1":   skip_same_torrent = True
            elif choice == "2": skip_same_torrent = False
            else:               modify_skip_same = False

        print("\n[7/8] 是否需要修改RSS抓取间隔(cron)? (y/n)")
        modify_cron = input(">>> ").strip().lower() == "y"
        rss_cron = None
        if modify_cron:
            cron_options = {
                "1": "*/5 * * * * *",  "2": "*/46 * * * * *",
                "3": "* * * * *",      "4": "*/5 * * * *",
                "5": "*/10 * * * *",   "6": "*/30 * * * *",
            }
            print("1.每5秒  2.每46秒  3.每1分钟  4.每5分钟  5.每10分钟  6.每30分钟  7.自定义")
            choice = input("请输入选项(1-7): ").strip()
            if choice in cron_options:
                rss_cron = cron_options[choice]
            elif choice == "7":
                rss_cron = input("请输入自定义cron表达式: ").strip() or None
            if not rss_cron:
                modify_cron = False

        print("\n[8/8] 是否需要修改下载器列表(clientArr)? (y/n)")
        modify_client_arr = input(">>> ").strip().lower() == "y"
        client_arr = None
        if modify_client_arr:
            user_input = input("请输入下载器ID（逗号分隔，留空=清空）: ").strip()
            client_arr = [cid.strip() for cid in user_input.split(",") if cid.strip()] if user_input else []

        if not any([modify_sort, modify_max_download, modify_skip_same, modify_cron, modify_client_arr]):
            print("\n❌ 未选择任何修改项，操作取消")
            return

        print(f"\n即将修改 {len(filtered_tasks)} 个RSS任务:")
        if modify_sort:         print(f"  - 排序方式: {client_sort_by}")
        if modify_max_download: print(f"  - 下载器任务上限: {max_client_download_count or '不限制'}")
        if modify_skip_same:    print(f"  - 跳过相同种子: {'启用' if skip_same_torrent else '禁用'}")
        if modify_cron:         print(f"  - RSS抓取间隔: {rss_cron}")
        if modify_client_arr:   print(f"  - 下载器列表: {client_arr if client_arr else '（清空）'}")

        if input("\n确认继续? (y/n): ").strip().lower() != "y":
            print("❌ 操作已取消")
            return

        success_count = fail_count = 0
        for task in filtered_tasks:
            print(f"\n正在修改: {task['alias']}")
            result = self.modify_rss_task(
                task,
                client_sort_by=client_sort_by if modify_sort else None,
                max_client_download_count=max_client_download_count if modify_max_download else None,
                skip_same_torrent=skip_same_torrent if modify_skip_same else None,
                cron=rss_cron if modify_cron else None,
                client_arr=client_arr if modify_client_arr else None,
            )
            if result and result.get("success"):
                print("  ✓ 修改成功"); success_count += 1
            else:
                print("  ✗ 修改失败"); fail_count += 1

        print("\n" + "=" * 60)
        print(f"修改完成！  成功: {success_count}  失败: {fail_count}")
        print("=" * 60)


# ══════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("Vertex 批量修改工具  (Cookie 自动刷新版)")
    print("=" * 60)

    # 构建 Cookie 提供者
    if _COOKIE_MODULE_AVAILABLE:
        manager  = build_cookie_manager()
        provider = CookieProvider(manager=manager)
        base_url = manager.login_url if manager else "http://23.82.99.203:3077"
    else:
        # 回退：从 cookies.txt 读取
        provider = CookieProvider(manager=None, cookies_file="cookies.txt")
        base_url = "http://23.82.99.203:3077"   # 无 manager 时用默认地址

    print("\n请选择要使用的功能:")
    print("1. 修改 Downloader（下载器）")
    print("2. 修改 RSS 任务")
    choice = input("\n请输入选项 (1 或 2): ").strip()

    if choice == "1":
        modifier = VertexModifier(provider, base_url, filter_keyword="NC")
        modifier.run()
    elif choice == "2":
        rss_modifier = RSSModifier(provider, base_url, filter_keyword="")
        rss_modifier.run()
    else:
        print("❌ 无效的选项，程序退出")