"""
qbittorrent_manager.py — qBittorrent 批量管理工具
===================================================
两种入口：
  模式 1 (直连)   : 直接用 qB 用户名+密码登录 qB Web UI
  模式 2 (Vertex) : 通过 Vertex 反代链接 + Cookie 访问 qB API
                    代理 URL 格式: http://<vertex_host>/proxy/client/<下载器id>/
                    所有 API 请求带上 Vertex Cookie 即可，无需 qB 密码

依赖: vertex_cookie.py（与本文件同目录）
"""

import requests
from typing import List, Optional
from datetime import datetime
from vertex_cookie import VertexCookieManager


# ══════════════════════════════════════════════════════════════════════════════
#  配置区（按实际情况修改）
# ══════════════════════════════════════════════════════════════════════════════

# ── 模式 1：qB 直连 ──────────────────────────────────────────────────────────
QB_HOST     = "http://192.227.220.73:9090"
QB_USERNAME = "heshui"
QB_PASSWORD = "1wuhongli"

# ── 模式 2：Vertex 代理 ──────────────────────────────────────────────────────
VERTEX_URL       = "http://23.82.99.203:3077"
VERTEX_USERNAME  = "admin"
VERTEX_PASSWORD  = "713aa2ac-ddd5-403a-9ddd-4132ce55289a"  # 明文，自动 MD5
VERTEX_CLIENT_ID = "4de2521c"   # Vertex 中的下载器 ID

# 代理 URL 由 VERTEX_URL + VERTEX_CLIENT_ID 自动拼合，无需手动填写：
#   http://ip:port/proxy/client/d85456e1/

# ══════════════════════════════════════════════════════════════════════════════


def _build_vertex_proxy_url(vertex_url: str, client_id: str) -> str:
    """拼合 Vertex 反代 base URL"""
    return f"{vertex_url.rstrip('/')}/proxy/client/{client_id.strip('/')}"


class QBittorrentClient:
    """
    qBittorrent 客户端，支持两种认证模式：

    模式 1 — qB 直连（username + password）
        client = QBittorrentClient.direct(QB_HOST, QB_USERNAME, QB_PASSWORD)

    模式 2 — Vertex 代理（Cookie 自动刷新）
        vcm    = VertexCookieManager(VERTEX_URL, VERTEX_USERNAME, VERTEX_PASSWORD)
        client = QBittorrentClient.via_vertex(VERTEX_URL, VERTEX_CLIENT_ID, vcm)
    """

    def __init__(self, host: str, vcm: Optional[VertexCookieManager] = None):
        """
        内部构造器，请使用 direct() 或 via_vertex() 工厂方法。

        :param host: qB Web UI 地址（直连）或 Vertex 反代地址（代理模式）
        :param vcm:  VertexCookieManager 实例（代理模式）；None 表示直连模式
        """
        self.host    = host.rstrip("/")
        self._vcm    = vcm
        self._mode   = "vertex" if vcm else "direct"
        self.session = requests.Session()

    # ── 工厂方法 ──────────────────────────────────────────────────────────────

    @classmethod
    def direct(cls, host: str, username: str, password: str) -> "QBittorrentClient":
        """qB 用户名+密码直连"""
        obj = cls(host, vcm=None)
        obj._login(username, password)
        print(f"✓ 认证模式: qB 直连  ({host})")
        obj._detect_version()
        return obj

    @classmethod
    def via_vertex(
        cls,
        vertex_url: str,
        client_id: str,
        vcm: VertexCookieManager,
    ) -> "QBittorrentClient":
        """
        Vertex 反代模式。
        host 自动设为 http://<vertex>/proxy/client/<client_id>
        所有请求带 Vertex Cookie，qB 侧无需额外认证。
        """
        proxy_host = _build_vertex_proxy_url(vertex_url, client_id)
        obj = cls(proxy_host, vcm=vcm)
        print(f"✓ 认证模式: Vertex 代理  ({proxy_host})")
        obj._detect_version()
        return obj

    # ── 版本探测（自动兼容 4.x / 5.x API 差异） ──────────────────────────────

    def _detect_version(self):
        """
        探测 qB 版本，自动适配接口名称差异：
          qB 4.x : pause / resume
          qB 5.x : stop  / start
        """
        try:
            r = self._get("/api/v2/app/version")
            ver_str = r.text.strip()          # 例如 "v5.0.4" 或 "v4.3.9"
            major = int(ver_str.lstrip("vV").split(".")[0])
            self._qb_major = major
            if major >= 5:
                self._api_pause  = "/api/v2/torrents/stop"
                self._api_resume = "/api/v2/torrents/start"
            else:
                self._api_pause  = "/api/v2/torrents/pause"
                self._api_resume = "/api/v2/torrents/resume"
            print(f"✓ qBittorrent 版本: {ver_str}  (接口: {'stop/start' if major >= 5 else 'pause/resume'})")
        except Exception as e:
            # 探测失败时保守回落到 4.x 接口
            self._qb_major   = 4
            self._api_pause  = "/api/v2/torrents/pause"
            self._api_resume = "/api/v2/torrents/resume"
            print(f"⚠ 版本探测失败({e})，回落到 4.x 接口")

    # ── 认证（直连模式用） ────────────────────────────────────────────────────

    def _login(self, username: str, password: str):
        resp = self.session.post(
            f"{self.host}/api/v2/auth/login",
            data={"username": username, "password": password},
        )
        if resp.text.strip() != "Ok.":
            raise Exception(f"qB 登录失败: {resp.text}")

    # ── HTTP 封装 ─────────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        if self._mode == "vertex":
            return {"Cookie": self._vcm.get_valid_cookie()}
        return {}

    def _get(self, path: str, **kwargs) -> requests.Response:
        url = f"{self.host}{path}"
        headers = {**self._headers(), **kwargs.pop("headers", {})}
        resp = self.session.get(url, headers=headers, **kwargs)
        if resp.status_code in (401, 403) and self._mode == "vertex":
            resp = self.session.get(
                url, headers={"Cookie": self._vcm.force_refresh()}, **kwargs
            )
        return resp

    def _post(self, path: str, **kwargs) -> requests.Response:
        url = f"{self.host}{path}"
        headers = {**self._headers(), **kwargs.pop("headers", {})}
        resp = self.session.post(url, headers=headers, **kwargs)
        if resp.status_code in (401, 403) and self._mode == "vertex":
            resp = self.session.post(
                url, headers={"Cookie": self._vcm.force_refresh()}, **kwargs
            )
        return resp

    # ── 格式化工具 ────────────────────────────────────────────────────────────

    def format_size(self, b: int) -> str:
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if b < 1024.0:
                return f"{b:.2f} {unit}"
            b /= 1024.0
        return f"{b:.2f} PB"

    def format_speed(self, s: int) -> str:
        return f"{self.format_size(s)}/s"

    def format_time(self, ts: int) -> str:
        return "N/A" if ts <= 0 else datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

    def get_state_cn(self, state: str) -> str:
        return {
            "downloading":        "下载中",
            "uploading":          "上传中",
            "stalledDL":          "等待下载",
            "stalledUP":          "等待上传",
            "pausedDL":           "暂停下载",
            "pausedUP":           "暂停上传",
            "queuedDL":           "队列下载",
            "queuedUP":           "队列上传",
            "checkingDL":         "检查下载",
            "checkingUP":         "检查上传",
            "checkingResumeData": "检查恢复数据",
            "error":              "错误",
            "missingFiles":       "文件丢失",
            "allocating":         "分配空间",
            "metaDL":             "下载元数据",
            "forcedDL":           "强制下载",
            "forcedUP":           "强制上传",
        }.get(state, state)

    # ── 种子 API ──────────────────────────────────────────────────────────────

    def get_torrents(self, filter_status: Optional[str] = None) -> List[dict]:
        params = {"filter": filter_status} if filter_status else {}
        r = self._get("/api/v2/torrents/info", params=params)
        r.raise_for_status()
        return r.json()

    def delete_torrents(self, hashes: List[str], delete_files: bool = False):
        self._post(
            "/api/v2/torrents/delete",
            data={"hashes": "|".join(hashes), "deleteFiles": str(delete_files).lower()},
        ).raise_for_status()

    def pause_torrents(self, hashes: List[str]):
        self._post(self._api_pause, data={"hashes": "|".join(hashes)}).raise_for_status()
        print(f"✓ 已暂停 {len(hashes)} 个种子")

    def resume_torrents(self, hashes: List[str]):
        self._post(self._api_resume, data={"hashes": "|".join(hashes)}).raise_for_status()
        print(f"✓ 已恢复 {len(hashes)} 个种子")

    def recheck_torrents(self, hashes: List[str]):
        self._post("/api/v2/torrents/recheck", data={"hashes": "|".join(hashes)}).raise_for_status()
        print(f"✓ 已开始重新校验 {len(hashes)} 个种子")

    def set_category(self, hashes: List[str], category: str):
        self._post(
            "/api/v2/torrents/setCategory",
            data={"hashes": "|".join(hashes), "category": category},
        ).raise_for_status()
        print(f"✓ 已将 {len(hashes)} 个种子设置分类为: {category}")

    def add_tags(self, hashes: List[str], tags: str):
        self._post(
            "/api/v2/torrents/addTags",
            data={"hashes": "|".join(hashes), "tags": tags},
        ).raise_for_status()
        print(f"✓ 已为 {len(hashes)} 个种子添加标签: {tags}")

    def set_download_limit(self, hashes: List[str], limit: int):
        self._post(
            "/api/v2/torrents/setDownloadLimit",
            data={"hashes": "|".join(hashes), "limit": limit},
        ).raise_for_status()
        print(f"✓ 下载限速已设为: {self.format_speed(limit) if limit > 0 else '不限速'}")

    # ── 展示 ──────────────────────────────────────────────────────────────────

    def _print_torrent(self, idx: int, t: dict, detailed: bool = False):
        print(f"【{idx:>3}】 {t['name']}")
        print(f"       状态: {self.get_state_cn(t['state']):<12} | 进度: {t['progress'] * 100:>6.2f}%")
        print(f"       大小: {self.format_size(t['size']):<12} | 已下载: {self.format_size(t['downloaded'])}")
        print(f"       ↓速度: {self.format_speed(t['dlspeed']):<15} | ↑速度: {self.format_speed(t['upspeed'])}")
        if detailed:
            print(f"       分类: {t.get('category', 'N/A'):<12} | 标签: {t.get('tags', 'N/A')}")
            print(f"       做种数: {t['num_seeds']:<10} | 下载数: {t['num_leechs']}")
            print(f"       分享率: {t['ratio']:.2f}")
            print(f"       添加时间: {self.format_time(t['added_on'])}")
            print(f"       完成时间: {self.format_time(t['completion_on'])}")
            print(f"       保存路径: {t['save_path']}")
            print(f"       哈希值: {t['hash']}")
        print(f"       {'-' * 93}")

    def list_torrents(self, filter_status: Optional[str] = None, detailed: bool = False) -> List[dict]:
        torrents = self.get_torrents(filter_status)
        if not torrents:
            print(f"\n没有找到状态为 '{filter_status or 'all'}' 的种子")
            return []
        print(f"\n{'=' * 100}")
        print(f"共找到 {len(torrents)} 个种子")
        print(f"{'=' * 100}\n")
        for i, t in enumerate(torrents, 1):
            self._print_torrent(i, t, detailed)
        return torrents

    def get_statistics(self):
        torrents = self.get_torrents()
        print(f"\n{'=' * 60}")
        print("qBittorrent 统计信息")
        print(f"{'=' * 60}")
        print(f"种子总数: {len(torrents)}")
        print(f"总大小:   {self.format_size(sum(t['size'] for t in torrents))}")
        print(f"已下载:   {self.format_size(sum(t['downloaded'] for t in torrents))}")
        print(f"已上传:   {self.format_size(sum(t['uploaded'] for t in torrents))}")
        print(f"当前↓速度: {self.format_speed(sum(t['dlspeed'] for t in torrents))}")
        print(f"当前↑速度: {self.format_speed(sum(t['upspeed'] for t in torrents))}")
        states: dict = {}
        for t in torrents:
            s = self.get_state_cn(t["state"])
            states[s] = states.get(s, 0) + 1
        print("\n状态分布:")
        for s, c in sorted(states.items(), key=lambda x: x[1], reverse=True):
            print(f"  {s}: {c} 个")
        print(f"{'=' * 60}\n")

    def batch_delete(self, filter_status: Optional[str] = None, delete_files: bool = False):
        torrents = self.list_torrents(filter_status)
        if not torrents:
            return
        action = "删除种子和文件" if delete_files else "仅删除种子(保留文件)"
        print(f"\n⚠️  即将 {action}")
        if input("确定要继续吗? (输入 yes 确认): ").lower() != "yes":
            print("✗ 已取消操作")
            return
        self.delete_torrents([t["hash"] for t in torrents], delete_files)
        print(f"\n✓ 成功删除 {len(torrents)} 个种子!")

    # ── 进度筛选 ──────────────────────────────────────────────────────────────

    def filter_by_progress(
        self,
        max_progress: float,
        min_progress: float = 0.0,
        filter_status: Optional[str] = None,
        detailed: bool = False,
    ) -> List[dict]:
        filtered = [
            t for t in self.get_torrents(filter_status)
            if min_progress / 100.0 <= t["progress"] < max_progress / 100.0
        ]
        if not filtered:
            print(f"\n没有找到进度在 {min_progress}% ~ {max_progress}% 之间的种子")
            return []
        print(f"\n{'=' * 100}")
        print(f"进度筛选: {min_progress}% ≤ 进度 < {max_progress}%  |  共找到 {len(filtered)} 个种子")
        print(f"{'=' * 100}\n")
        for i, t in enumerate(filtered, 1):
            self._print_torrent(i, t, detailed)
        return filtered

    def _select_from_list(self, torrents: List[dict]) -> List[dict]:
        """交互式选择：all / 单号 / 范围 1-5 / 逗号组合 1,3-5,8"""
        print(f"\n请选择要操作的种子 (共 {len(torrents)} 个):")
        print("  all / 3 / 1-5 / 1,3,7 / 1,3-5,8")
        raw = input("\n请输入选择: ").strip()
        if raw.lower() == "all":
            return torrents
        selected, seen = [], set()
        try:
            for part in raw.split(","):
                part = part.strip()
                if "-" in part:
                    s, e = part.split("-", 1)
                    for idx in range(int(s), int(e) + 1):
                        if 1 <= idx <= len(torrents) and idx not in seen:
                            selected.append(torrents[idx - 1])
                            seen.add(idx)
                else:
                    idx = int(part)
                    if 1 <= idx <= len(torrents) and idx not in seen:
                        selected.append(torrents[idx - 1])
                        seen.add(idx)
        except ValueError:
            print("✗ 输入格式有误，已取消")
            return []
        if selected:
            print(f"\n✓ 已选中 {len(selected)} 个种子:")
            for t in selected:
                print(f"   • [{t['progress']*100:.2f}%] {t['name']}")
        else:
            print("✗ 未选中任何种子")
        return selected

    def progress_filter_menu(self):
        """进度筛选菜单：筛选 → 选择子集 → 操作"""
        print("\n── 进度筛选设置 ──")
        try:
            max_p = float(input("进度上限 % (例如 50 表示 <50%): ").strip())
            raw   = input("进度下限 % (回车默认 0): ").strip()
            min_p = float(raw) if raw else 0.0
        except ValueError:
            print("✗ 请输入有效数字")
            return
        if not (0 <= min_p < max_p <= 100):
            print("✗ 进度范围无效")
            return
        filter_status = _ask_status("进度筛选额外按状态过滤")
        filtered = self.filter_by_progress(max_p, min_p, filter_status)
        if not filtered:
            return
        selected = self._select_from_list(filtered)
        if not selected:
            return

        while True:
            print(f"\n── 对已选 {len(selected)} 个种子执行操作 ──")
            print("  1. 删除种子 (保留文件)")
            print("  2. 删除种子 (同时删除文件) ⚠️")
            print("  3. 暂停    4. 恢复    5. 重新校验")
            print("  6. 设置分类    7. 添加标签    8. 设置下载限速")
            print("  9. 重新选择子集    0. 返回主菜单")
            op     = input("\n请选择操作: ").strip()
            hashes = [t["hash"] for t in selected]

            if op == "0":
                break
            elif op == "1":
                if input(f"确认删除 {len(selected)} 个种子 (保留文件)? (yes确认): ").lower() == "yes":
                    self.delete_torrents(hashes, False)
                    print(f"✓ 已删除 {len(selected)} 个种子（文件已保留）"); break
            elif op == "2":
                if input(f"⚠️  确认同时删除 {len(selected)} 个种子及文件? (yes确认): ").lower() == "yes":
                    self.delete_torrents(hashes, True)
                    print(f"✓ 已删除 {len(selected)} 个种子及文件"); break
            elif op == "3":  self.pause_torrents(hashes)
            elif op == "4":  self.resume_torrents(hashes)
            elif op == "5":  self.recheck_torrents(hashes)
            elif op == "6":
                cat = input("请输入分类名称: ").strip()
                if cat: self.set_category(hashes, cat)
            elif op == "7":
                tags = input("请输入标签 (多个用逗号分隔): ").strip()
                if tags: self.add_tags(hashes, tags)
            elif op == "8":
                try:
                    kb = float(input("请输入下载限速 KB/s (0=不限速): ").strip())
                    self.set_download_limit(hashes, int(kb * 1024))
                except ValueError:
                    print("✗ 请输入有效数字")
            elif op == "9":
                selected = self._select_from_list(filtered)
                if not selected: break
            else:
                print("✗ 无效选择")


# ══════════════════════════════════════════════════════════════════════════════
#  启动入口
# ══════════════════════════════════════════════════════════════════════════════

_STATUS_HINT = """\
  可用状态过滤值:
    downloading  下载中       uploading    上传中(做种)
    stalledDL    等待下载     stalledUP    等待上传
    pausedDL     暂停下载     pausedUP     暂停上传
    queuedDL     队列下载     queuedUP     队列上传
    checkingDL   检查下载     checkingUP   检查上传
    error        错误         missingFiles 文件丢失
    metaDL       下载元数据   allocating   分配空间
    forcedDL     强制下载     forcedUP     强制上传
  ※ 回车不输入 = 全部状态"""


def _ask_status(prompt: str = "按状态过滤") -> Optional[str]:
    print(_STATUS_HINT)
    s = input(f"{prompt} (回车=全部): ").strip()
    return s or None


def choose_client() -> QBittorrentClient:
    print("=" * 60)
    print("qBittorrent 批量管理工具")
    print("=" * 60)
    print("\n请选择连接方式:")
    print("  1. Vertex 代理（Cookie 自动刷新）  ← 默认")
    print(f"     → {VERTEX_URL}/proxy/client/{VERTEX_CLIENT_ID}/")
    print("  2. qB 直连（用户名+密码）")
    print(f"     → {QB_HOST}")
    print()

    choice = input("请输入选择 (回车=默认 1): ").strip()

    if choice == "2":
        return QBittorrentClient.direct(QB_HOST, QB_USERNAME, QB_PASSWORD)
    else:
        vcm = VertexCookieManager(VERTEX_URL, VERTEX_USERNAME, VERTEX_PASSWORD)
        return QBittorrentClient.via_vertex(VERTEX_URL, VERTEX_CLIENT_ID, vcm)


def main():
    try:
        client = choose_client()

        while True:
            print("\n" + "=" * 60)
            print("qBittorrent 批量管理工具")
            print("=" * 60)
            print("1. 查看所有种子         2. 按状态查看种子")
            print("3. 查看详细信息         4. 查看统计信息")
            print("5. 批量删除(保留文件)   6. 批量删除(同时删除文件)")
            print("7. 批量暂停             8. 批量恢复")
            print("─" * 60)
            print("9. 🔍 按进度筛选种子")
            print("─" * 60)
            print("0. 退出")
            print("=" * 60)

            choice = input("\n请选择操作 (0-9): ").strip()

            if choice == "0":
                print("\n再见!")
                break
            elif choice == "1":
                client.list_torrents()
            elif choice == "2":
                s = _ask_status("按状态查看")
                client.list_torrents(s)
            elif choice == "3":
                s = _ask_status("按状态过滤")
                client.list_torrents(s, detailed=True)
            elif choice == "4":
                client.get_statistics()
            elif choice == "5":
                s = _ask_status("删除哪些状态的种子")
                client.batch_delete(s, delete_files=False)
            elif choice == "6":
                print("⚠️  警告: 此操作将同时删除下载的文件!")
                s = _ask_status("删除哪些状态的种子")
                client.batch_delete(s, delete_files=True)
            elif choice == "7":
                s = _ask_status("暂停哪些状态的种子")
                torrents = client.get_torrents(s)
                if torrents: client.pause_torrents([t["hash"] for t in torrents])
            elif choice == "8":
                s = _ask_status("恢复哪些状态的种子")
                torrents = client.get_torrents(s)
                if torrents: client.resume_torrents([t["hash"] for t in torrents])
            elif choice == "9":
                client.progress_filter_menu()
            else:
                print("\n✗ 无效选择，请重试")

    except KeyboardInterrupt:
        print("\n\n程序已中断")
    except Exception as e:
        print(f"\n✗ 错误: {e}")


if __name__ == "__main__":
    main()