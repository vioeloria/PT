"""
U2 双功能监控脚本
- 功能A: CatchMagic  RSS -> http://<ip>:8787/rss_magic.xml
- 功能B: TorrentList RSS -> http://<ip>:8788/rss_list.xml

依赖: pip install requests lxml bs4 loguru pytz
"""

import gc, json, re, pytz
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from time import sleep, time
import xml.etree.ElementTree as ET
from xml.dom import minidom

from requests import get, ReadTimeout, ConnectTimeout
from bs4 import BeautifulSoup
from loguru import logger

# ============================================================
# 基础配置
# ============================================================
COOKIES   = {'nexusphp_u2': ''}
INTERVAL      = 60    # 魔法检查间隔(秒)
LIST_INTERVAL = 120   # 列表轮询间隔(秒)
API_TOKEN = ''
UID       = 58929
PROXIES   = {'http': '', 'https': ''}

# ============================================================
# CatchMagic 过滤规则
# ============================================================
MAX_SEEDER_NUM    = 5
DOWNLOAD_NON_FREE = False
MIN_DAY           = 7
DOWNLOAD_OLD      = True
DOWNLOAD_NEW      = False
MAGIC_SELF        = False
EFFECTIVE_DELAY   = 60
DOWNLOAD_DEAD_TO  = False
CHECK_PEERLIST    = False
DA_QIAO           = True
MIN_RE_DL_DAYS    = 0
CAT_FILTER        = []
SIZE_FILTER       = [0, -1]
NAME_FILTER       = []

# ============================================================
# 列表爬取目标 URL
# ============================================================
LIST_URLS = [
    "https://u2.dmhy.org/torrents.php?incldead=1&spstate=4&sort=4&type=desc",
    "https://u2.dmhy.org/torrents.php?incldead=1&spstate=2&sort=4&type=desc",
]

# ============================================================
# TorrentList 过滤参数
# ============================================================
LIST_MAX_SEEDERS     = 3    # 做种数严格小于此值（即 0、1、2 才录入）
LIST_MAX_AGE_MINUTES = 10   # 发布时间在此分钟数以内才录入

# ============================================================
# RSS 配置
# ============================================================
RSS_MAX_ITEMS = 200

MAGIC_RSS_TITLE       = 'U2 CatchMagic'
MAGIC_RSS_LINK        = 'https://u2.dmhy.org/'
MAGIC_RSS_DESCRIPTION = 'U2 魔法促销 RSS，供 Vertex 订阅。'
MAGIC_RSS_HTTP_BIND   = '0.0.0.0'
MAGIC_RSS_HTTP_PORT   = 8787

LIST_RSS_TITLE        = 'U2 Free/2xFree Torrents'
LIST_RSS_LINK         = 'https://u2.dmhy.org/'
LIST_RSS_DESCRIPTION  = 'U2 Free/2xFree 种子列表 RSS，供 Vertex 订阅。'
LIST_RSS_HTTP_BIND    = '0.0.0.0'
LIST_RSS_HTTP_PORT    = 8788

# ============================================================
# 路径
# ============================================================
DATA_DIR        = '.'
LOG_PATH        = f'{DATA_DIR}/u2_monitor.log'
MAGIC_DATA_PATH = f'{DATA_DIR}/catch_magic.data.txt'
MAGIC_RSS_PATH  = f'{DATA_DIR}/rss_magic.xml'
LIST_RSS_PATH   = f'{DATA_DIR}/rss_list.xml'
LIST_STATE_PATH = f'{DATA_DIR}/list_seen.json'

R_ARGS = {
    'cookies': COOKIES,
    'headers': {'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'},
    'timeout': 20,
    'proxies': PROXIES,
}
CST = pytz.timezone("Asia/Shanghai")

# ============================================================
# 公共 RSS 构建（仿照原 CatchMagic 结构，用 ElementTree）
# ============================================================
def build_rss_xml(title, link, description, items):
    rss     = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text       = title
    ET.SubElement(channel, "link").text        = link
    ET.SubElement(channel, "description").text = description
    ET.SubElement(channel, "language").text    = "zh-cn"
    ET.SubElement(channel, "ttl").text         = "60"
    ET.SubElement(channel, "pubDate").text     = format_datetime(datetime.now(CST))

    for it in items:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text       = it["title"]
        ET.SubElement(item, "link").text        = it["link"]
        ET.SubElement(item, "description").text = it.get("description", "")

        enc = ET.SubElement(item, "enclosure")
        enc.set("url",    it["enclosure"])
        enc.set("length", str(it.get("length", 0)))
        enc.set("type",   "application/x-bittorrent")

        guid = ET.SubElement(item, "guid")
        guid.set("isPermaLink", "false")
        guid.text = it["guid"]

        pub = it.get("pubDate")
        ET.SubElement(item, "pubDate").text = format_datetime(pub) if isinstance(pub, datetime) else str(pub or "")

        if it.get("category"):
            ET.SubElement(item, "category").text = it["category"]

    raw    = ET.tostring(rss, encoding="utf-8", xml_declaration=True)
    return minidom.parseString(raw).toprettyxml(indent="  ", encoding="utf-8")


def make_rss_handler(rss_path, write_fn):
    """工厂：生成绑定特定 RSS 文件的 HTTP Handler"""
    class Handler(BaseHTTPRequestHandler):
        def _serve(self):
            if self.path.split("?")[0] not in {"/", "/rss.xml", "/rss_magic.xml", "/rss_list.xml"}:
                self.send_response(404); self.end_headers(); return
            try:
                write_fn()
                with open(rss_path, 'rb') as f:
                    body = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/xml; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                if self.command == 'GET':
                    self.wfile.write(body)
            except Exception:
                self.send_response(500); self.end_headers()
        def do_GET(self):  self._serve()
        def do_HEAD(self): self._serve()
        def log_message(self, fmt, *args): pass
    return Handler


# ============================================================
# 功能A: CatchMagic
# ============================================================
class CatchMagic:
    pre_suf = [
        ['时区', '，点击修改。'],
        ['時區', '，點擊修改。'],
        ['Current timezone is ', ', click to change.'],
    ]

    def __init__(self):
        self.checked      = deque([], maxlen=200)
        self.magic_id_0   = None
        self.tid_add_time = {}
        self.rss_items    = deque([], maxlen=RSS_MAX_ITEMS)
        self.rss_guids    = deque([], maxlen=RSS_MAX_ITEMS)
        try:
            with open(MAGIC_DATA_PATH, 'r', encoding='utf-8') as fp:
                data = json.load(fp)
                self.checked      = deque(data.get('checked', []),   maxlen=200)
                self.magic_id_0   = data.get('id_0')
                self.tid_add_time = data.get('add_time', {})
                self.rss_items    = deque(data.get('rss_items', []), maxlen=RSS_MAX_ITEMS)
                self.rss_guids    = deque(data.get('rss_guids', []), maxlen=RSS_MAX_ITEMS)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        self.first_time = True

    def info_from_u2(self):
        all_checked = True if self.first_time and not self.magic_id_0 else False
        index = 0
        id_0  = self.magic_id_0
        while True:
            soup    = self.get_soup(f'https://u2.dmhy.org/promotion.php?action=list&page={index}')
            user_id = soup.find('table', {'id': 'info_block'}).a['href'][19:]
            for i, tr in filter(lambda tup: tup[0] > 0, enumerate(soup.find('table', {'width': '99%'}))):
                magic_id = int(tr.contents[0].string)
                if index == 0 and i == 1:
                    self.magic_id_0 = magic_id
                    if self.first_time and id_0 and magic_id - id_0 > 10 * INTERVAL:
                        all_checked = True
                if tr.contents[5].string in ['Expired', '已失效'] or magic_id == id_0:
                    all_checked = True; break
                if tr.contents[1].string in ['魔法', 'Magic', 'БР']:
                    if not tr.contents[3].a and tr.contents[3].string in ['所有人', 'Everyone', 'Для всех'] \
                            or MAGIC_SELF and tr.contents[3].a and tr.contents[3].a['href'][19:] == user_id:
                        if tr.contents[5].string not in ['Terminated', '终止', '終止', 'Прекращён']:
                            if tr.contents[2].a:
                                tid = int(tr.contents[2].a['href'][15:])
                                if magic_id not in self.checked:
                                    if self.first_time and all_checked:
                                        self.checked.append(magic_id)
                                    else:
                                        yield magic_id, tid
                                    continue
                if magic_id not in self.checked:
                    self.checked.append(magic_id)
            if all_checked:
                break
            else:
                index += 1

    def info_from_api(self):
        r_args   = {'timeout': R_ARGS.get('timeout'), 'proxies': R_ARGS.get('proxies')}
        params   = {'uid': UID, 'token': API_TOKEN, 'scope': 'public', 'maximum': 30}
        resp     = get('https://u2.kysdm.com/api/v1/promotion', **r_args, params=params).json()
        pro_list = resp['data']['promotion']
        if MAGIC_SELF:
            params['scope'] = 'private'
            resp1 = get('https://u2.kysdm.com/api/v1/promotion', **r_args, params=params).json()
            pro_list.extend([p for p in resp1['data']['promotion'] if p['for_user_id'] == UID])
        for pro_data in pro_list:
            magic_id = pro_data['promotion_id']
            tid      = pro_data['torrent_id']
            if magic_id == self.magic_id_0: break
            if magic_id not in self.checked:
                if self.first_time and not self.magic_id_0:
                    self.checked.append(magic_id)
                else:
                    yield magic_id, tid
        self.magic_id_0 = pro_list[0]['promotion_id']

    def all_effective_magic(self):
        id_0 = self.magic_id_0
        if not API_TOKEN:
            yield from self.info_from_u2()
        else:
            try:
                yield from self.info_from_api()
            except Exception as e:
                logger.exception(e)
                yield from self.info_from_u2()
        if self.magic_id_0 != id_0:
            self.save_data()
        self.first_time = False

    def save_data(self):
        with open(MAGIC_DATA_PATH, 'w', encoding='utf-8') as fp:
            json.dump({
                'checked':   list(self.checked),
                'id_0':      self.magic_id_0,
                'add_time':  self.tid_add_time,
                'rss_items': list(self.rss_items),
                'rss_guids': list(self.rss_guids),
            }, fp, ensure_ascii=False, default=str)

    def _append_rss_item(self, magic_id, tid, to_name, length=0):
        guid = f'u2:magic:{magic_id}:tid:{tid}'
        if guid in self.rss_guids: return
        self.rss_items.appendleft({
            "title":       f"[U2][Magic {magic_id}] {to_name} (tid={tid})",
            "link":        f"https://u2.dmhy.org/details.php?id={tid}",
            "enclosure":   f"https://u2.dmhy.org/download.php?id={tid}&https=1",
            "length":      int(length) if length else 0,
            "guid":        guid,
            "pubDate":     datetime.now(CST),
            "description": f"magic_id={magic_id} tid={tid}",
        })
        self.rss_guids.appendleft(guid)

    def write_rss(self):
        data = build_rss_xml(MAGIC_RSS_TITLE, MAGIC_RSS_LINK, MAGIC_RSS_DESCRIPTION, list(self.rss_items))
        with open(MAGIC_RSS_PATH, 'wb') as f: f.write(data)

    def start_rss_http(self):
        handler = make_rss_handler(MAGIC_RSS_PATH, self.write_rss)
        server  = HTTPServer((MAGIC_RSS_HTTP_BIND, MAGIC_RSS_HTTP_PORT), handler)
        self.write_rss()
        Thread(target=server.serve_forever, daemon=True).start()
        logger.info(f'[Magic] RSS HTTP -> http://0.0.0.0:{MAGIC_RSS_HTTP_PORT}/rss_magic.xml')

    def process_torrent(self, to_info):
        tid = to_info['dl_link'].split('&passkey')[0].split('id=')[1]
        if tid in self.tid_add_time:
            logger.info(f'Torrent {tid} | Already processed.'); return
        if CHECK_PEERLIST and to_info.get('last_dl_time'):
            peer_list = self.get_soup(f'https://u2.dmhy.org/viewpeerlist.php?id={tid}')
            for table in peer_list.find_all('table') or []:
                for tr in filter(lambda _tr: 'nowrap' in str(_tr), table):
                    if tr.get('bgcolor'):
                        logger.info(f"Torrent {tid} | Already seeding/downloading."); return
        self._append_rss_item(magic_id=to_info.get('magic_id', 0), tid=tid,
                               to_name=to_info['to_name'], length=to_info.get("length", 0))
        self.write_rss()
        logger.info(f"[Magic] Added tid={tid}  {to_info['to_name']}")
        self.tid_add_time[tid] = time()

    @classmethod
    def get_tz(cls, soup):
        tz_info = soup.find('a', {'href': 'usercp.php?action=tracker#timezone'})['title']
        tz = [tz_info[len(pre):-len(suf)].strip() for pre, suf in cls.pre_suf if tz_info.startswith(pre)][0]
        return pytz.timezone(tz)

    @staticmethod
    def timedelta(date, timezone):
        dt = datetime.strptime(date, '%Y-%m-%d %H:%M:%S')
        return time() - timezone.localize(dt).timestamp()

    @staticmethod
    def get_pro(td):
        pro = {'ur': 1.0, 'dr': 1.0}
        pro_dict = {'free': {'dr': 0.0}, '2up': {'ur': 2.0}, '50pct': {'dr': 0.5}, '30pct': {'dr': 0.3}, 'custom': {}}
        for img in td.select('img') or []:
            if not [pro.update(data) for key, data in pro_dict.items() if key in img['class'][0]]:
                pro[{'arrowup': 'ur', 'arrowdown': 'dr'}[img['class'][0]]] = float(img.next.text[:-1].replace(',', '.'))
        return list(pro.values())

    @staticmethod
    def get_soup(url):
        html = get(url, **R_ARGS).text
        return BeautifulSoup(html.replace('\n', ''), 'lxml')

    def analyze_magic(self, magic_id, tid):
        soup = self.get_soup(f'https://u2.dmhy.org/details.php?id={tid}')
        aa   = soup.select('a.index')
        if len(aa) < 2:
            logger.info(f'Torrent {tid} | deleted, passed'); return
        to_info = {'to_name': aa[0].text[5:-8],
                   'dl_link': f"https://u2.dmhy.org/{aa[1]['href']}",
                   'magic_id': magic_id}
        if NAME_FILTER and any(st in to_info['to_name'] for st in NAME_FILTER): return
        if CAT_FILTER and soup.time.parent.contents[7].strip() not in CAT_FILTER: return
        if SIZE_FILTER and not (SIZE_FILTER[0] <= 0 and SIZE_FILTER[1] == -1):
            size_str = soup.time.parent.contents[5].strip().replace(',', '.').replace('Б', 'B')
            num, unit = size_str.split(' ')
            _pow = ['MiB','GiB','TiB','喵','寄','烫','egamay','igagay','eratay'].index(unit) % 3
            gb = float(num) * 1024 ** (_pow - 1)
            to_info["length"] = int(gb * 1024**3)
            if gb < SIZE_FILTER[0] or SIZE_FILTER[1] != -1 and gb > SIZE_FILTER[1]: return
        if CHECK_PEERLIST or MIN_RE_DL_DAYS > 0:
            for tr in soup.find('table', {'width': '90%'}):
                if tr.td.text in ['My private torrent','私人种子文件','私人種子文件','Ваш личный торрент']:
                    time_str = tr.find_all('time')
                    to_info['last_dl_time'] = time() - self.timedelta(
                        time_str[1].get('title') or time_str[1].text, self.get_tz(soup)) if time_str else None
            if MIN_RE_DL_DAYS > 0 and to_info.get('last_dl_time') and \
               time() - to_info['last_dl_time'] < 86400 * MIN_RE_DL_DAYS: return
        delta        = self.timedelta(soup.time.get('title') or soup.time.text, self.get_tz(soup))
        seeder_count = int(re.search(r'(\d+)', soup.find('div', {'id': 'peercount'}).b.text).group(1))
        magic_page_soup = None
        promo_key = ['流量优惠','流量優惠','Promotion','Тип раздачи (Бонусы)']
        if delta < MIN_DAY * 86400:
            if DOWNLOAD_NEW and seeder_count <= MAX_SEEDER_NUM:
                if [self.get_pro(tr.contents[1])[1] for tr in soup.find('table', {'width': '90%'})
                        if tr.td.text in promo_key][0] <= 0:
                    self.process_torrent(to_info)
            return
        elif not DOWNLOAD_OLD:
            return
        if not DOWNLOAD_NON_FREE:
            if [self.get_pro(tr.contents[1])[1] for tr in soup.find('table', {'width': '90%'})
                    if tr.td.text in promo_key][0] > 0:
                magic_page_soup = self.get_soup(f'https://u2.dmhy.org/promotion.php?action=detail&id={magic_id}')
                tbody = magic_page_soup.find('table', {'width': '75%', 'cellpadding': 4}).tbody
                if self.get_pro(tbody.contents[6].contents[1])[1] == 0:
                    time_tag = tbody.contents[4].contents[1].time
                    delay = -self.timedelta(time_tag.get('title') or time_tag.text, self.get_tz(magic_page_soup))
                    if not (-1 < delay < EFFECTIVE_DELAY): return
                else:
                    return
        if seeder_count > 0 or DOWNLOAD_DEAD_TO:
            if seeder_count <= MAX_SEEDER_NUM:
                self.process_torrent(to_info)
            elif DA_QIAO:
                if not magic_page_soup:
                    magic_page_soup = self.get_soup(f'https://u2.dmhy.org/promotion.php?action=detail&id={magic_id}')
                comment = magic_page_soup.legend.parent.contents[1].text
                if '搭' in comment and '桥' in comment or '加' in comment and '速' in comment:
                    self.process_torrent(to_info)

    def run(self):
        id_0 = self.magic_id_0
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(self.analyze_magic, magic_id, tid): magic_id
                       for magic_id, tid in self.all_effective_magic()}
            if futures:
                error = False
                for future in as_completed(futures):
                    try:
                        future.result()
                        self.checked.append(futures[future])
                    except Exception as er:
                        error = True
                        if isinstance(er, (ReadTimeout, ConnectTimeout)):
                            logger.error(er)
                        else:
                            logger.exception(er)
                if error:
                    self.magic_id_0 = id_0
                self.save_data()


# ============================================================
# 功能B: TorrentList（Free/2xFree 列表爬取）
# ============================================================
PROMO_MAP = {
    '免费': 'Free', 'free': 'Free',
    '2xfree': '2xFree', '2x免费': '2xFree',
    '2x': '2x Upload',
    '50%': '50% Download', '30%': '30% Download',
}


def _parse_pubdate(time_tag) -> datetime | None:
    """
    从 <time title="2026-03-04 23:50:00"> 解析发布时间。
    优先取 title 属性（精确到秒），fallback 取文本内容。
    返回带时区的 datetime（CST），解析失败返回 None。
    """
    if not time_tag:
        return None
    raw = time_tag.get('title', '').strip()
    if not raw:
        raw = time_tag.get_text(strip=True)
    # 去除软连字符 ­ (U+00AD) 等不可见字符后再尝试解析
    raw = re.sub(r'[\u00ad\u200b\u200c\u200d\ufeff]', '', raw).strip()
    try:
        return CST.localize(datetime.strptime(raw, '%Y-%m-%d %H:%M:%S'))
    except ValueError:
        return None


def _age_minutes(pub_dt: datetime) -> float:
    """返回种子发布到现在的分钟数；pub_dt 为 None 时返回无穷大（视为超龄）。"""
    if pub_dt is None:
        return float('inf')
    now_cst = datetime.now(CST)
    delta   = now_cst - pub_dt
    return delta.total_seconds() / 60.0


def _parse_torrents_from_html(html):
    """
    解析种子列表页。
    标题取 <a class="tooltip" title="..."> 的 title 属性（完整文件名）。
    做种数取 <a href="details.php?id=xxx&hit=1&dllist=1#seeders"> 的文本数值。
    发布时间取第4列 <time title="YYYY-MM-DD HH:MM:SS">。
    """
    soup    = BeautifulSoup(html, 'lxml')
    results = []

    for tr in soup.select('tr'):
        tds = tr.find_all('td', class_='rowfollow')
        if len(tds) < 7:
            continue

        # 分类
        cat_a = tds[0].find('a')
        cat   = cat_a.get_text(strip=True) if cat_a else ''

        # 标题：取 class="tooltip" 的 <a> 的 title 属性
        title_a = tds[1].find('a', class_='tooltip')
        if not title_a:
            continue
        title = title_a.get('title', '').strip() or title_a.get_text(strip=True)

        # 种子 id
        m = re.search(r'id=(\d+)', title_a.get('href', ''))
        if not m:
            continue
        tid = m.group(1)

        # 促销类型
        promo        = ''
        promo_remain = ''
        for span in tds[1].find_all('span'):
            cls = ' '.join(span.get('class', []))
            txt = span.get_text(strip=True)
            if any(k in cls for k in ('free', 'twoup', 'halfdown', 'thirtydown')):
                promo = PROMO_MAP.get(txt, txt)
        time_tags = tds[1].find_all('time')
        if time_tags:
            promo_remain = time_tags[0].get('title', '') or time_tags[0].get_text(strip=True)

        # 发布时间（第4列，index=3）
        pub_dt   = None
        pub_time = tds[3].find('time')
        if pub_time:
            pub_dt = _parse_pubdate(pub_time)

        pubdate = pub_dt.strftime('%Y-%m-%d %H:%M:%S') if pub_dt else ''

        # 大小（第5列）
        size   = ''
        size_m = re.search(r'([\d.]+)\s*<br\s*/?>\s*(GiB|MiB|TiB|KiB|GB|MB)', str(tds[4]), re.I)
        if size_m:
            size = f"{size_m.group(1)} {size_m.group(2)}"
        else:
            size = tds[4].get_text(' ', strip=True)

        # ── 做种数：从 <a href="...#seeders"> 取文本，而非直接用列文本 ──
        # 结构示例：<a href="details.php?id=64136&hit=1&dllist=1#seeders"><font color="#ff0000">1</font></a>
        seeders_raw = tds[5].get_text(strip=True)   # 备用
        seeder_a    = tds[5].find('a', href=re.compile(r'#seeders'))
        if seeder_a:
            seeders_raw = seeder_a.get_text(strip=True)
        try:
            seeders_int = int(seeders_raw)
        except (ValueError, TypeError):
            seeders_int = None   # 解析失败时不过滤（保守策略）

        leechers = tds[6].get_text(strip=True)

        results.append({
            'id':          tid,
            'title':       title,
            'category':    cat,
            'promo':       promo,
            'promo_remain': promo_remain,
            'size':        size,
            'pubdate':     pubdate,
            'pub_dt':      pub_dt,          # datetime 对象，供过滤用
            'seeders':     seeders_raw,
            'seeders_int': seeders_int,     # int 或 None，供过滤用
            'leechers':    leechers,
        })

    return results


def _torrent_to_rss_item(t):
    tid   = t['id']
    title = t['title'] or f"Torrent #{tid}"
    promo = t['promo']
    cat   = t['category']

    # 标题格式: [U2][促销][分类] 完整种子名 (大小)
    display = '[U2]'
    if promo: display += f'[{promo}]'
    if cat:   display += f'[{cat}]'
    display += f' {title}'
    if t['size']: display += f' ({t["size"]})'

    desc_parts = [f'tid={tid}']
    if promo:              desc_parts.append(f'promo={promo}')
    if t['size']:          desc_parts.append(f'size={t["size"]}')
    if cat:                desc_parts.append(f'category={cat}')
    if t['seeders']:       desc_parts.append(f'seeders={t["seeders"]}')
    if t['leechers']:      desc_parts.append(f'leechers={t["leechers"]}')
    if t['promo_remain']:  desc_parts.append(f'promo_until={t["promo_remain"]}')

    pub_dt = t.get('pub_dt') or datetime.now(CST)

    return {
        'title':       display,
        'link':        f'https://u2.dmhy.org/details.php?id={tid}',
        'description': ' | '.join(desc_parts),
        'enclosure':   f'https://u2.dmhy.org/download.php?id={tid}&https=1',
        'length':      0,
        'guid':        f'u2:tid:{tid}',
        'pubDate':     pub_dt,
        'category':    cat,
    }


class TorrentListMonitor:
    def __init__(self):
        self.seen      = self._load_seen()
        self.rss_items = deque([], maxlen=RSS_MAX_ITEMS)

    @staticmethod
    def _load_seen():
        try:
            return set(json.loads(open(LIST_STATE_PATH, encoding='utf-8').read()))
        except Exception:
            return set()

    def _save_seen(self):
        with open(LIST_STATE_PATH, 'w', encoding='utf-8') as f:
            json.dump(list(self.seen), f)

    def write_rss(self):
        data = build_rss_xml(LIST_RSS_TITLE, LIST_RSS_LINK, LIST_RSS_DESCRIPTION, list(self.rss_items))
        with open(LIST_RSS_PATH, 'wb') as f: f.write(data)

    def start_rss_http(self):
        handler = make_rss_handler(LIST_RSS_PATH, self.write_rss)
        server  = HTTPServer((LIST_RSS_HTTP_BIND, LIST_RSS_HTTP_PORT), handler)
        self.write_rss()
        Thread(target=server.serve_forever, daemon=True).start()
        logger.info(f'[List]  RSS HTTP -> http://0.0.0.0:{LIST_RSS_HTTP_PORT}/rss_list.xml')

    @staticmethod
    def _passes_filters(t: dict, first: bool) -> tuple[bool, str]:
        """
        对单条种子执行过滤，返回 (通过, 原因说明)。
        first=True 时跳过时间过滤（首次加载历史数据不做年龄判断）。
        """
        tid = t['id']

        # ── 做种数过滤 ──────────────────────────────────────────
        s = t.get('seeders_int')
        if s is not None and s >= LIST_MAX_SEEDERS:
            return False, f"seeders={s} >= {LIST_MAX_SEEDERS}, skip"

        # ── 发布时间过滤（首次加载豁免）────────────────────────
        if not first:
            age = _age_minutes(t.get('pub_dt'))
            if age > LIST_MAX_AGE_MINUTES:
                return False, f"age={age:.1f}min > {LIST_MAX_AGE_MINUTES}min, skip"

        return True, ''

    def poll_once(self, first=False):
        fetched = {}
        for url in LIST_URLS:
            try:
                resp = get(url, **R_ARGS)
                resp.encoding = 'utf-8'
                torrents = _parse_torrents_from_html(resp.text)
                for t in torrents:
                    if t['id'] and t['id'] not in fetched:
                        fetched[t['id']] = t
                logger.debug(f"[List] {len(torrents)} items from {url.split('?')[1][:40]}")
            except Exception as e:
                logger.error(f"[List] Fetch error: {e}")

        new_count      = 0
        filtered_count = 0
        for tid, t in fetched.items():
            if tid in self.seen:
                continue

            passed, reason = self._passes_filters(t, first)
            if not passed:
                # 仍计入 seen，避免每轮重复判断同一条旧/高做种数种子
                self.seen.add(tid)
                filtered_count += 1
                if not first:
                    logger.debug(f"[List] SKIP tid={tid} | {reason} | {t['title'][:50]}")
                continue

            self.rss_items.appendleft(_torrent_to_rss_item(t))
            self.seen.add(tid)
            new_count += 1
            if not first:
                logger.info(
                    f"[List] NEW tid={tid} [{t['promo']}] "
                    f"seeders={t['seeders']} age={_age_minutes(t.get('pub_dt')):.1f}min "
                    f"size={t['size']} | {t['title'][:60]}"
                )

        if new_count:
            self.write_rss()
            self._save_seen()
            if not first:
                logger.info(f"[List] RSS updated, {new_count} new torrent(s) added, {filtered_count} filtered.")
        else:
            if not first:
                logger.info(f"[List] No new torrents. ({filtered_count} filtered this round)")

    def run_loop(self):
        self.poll_once(first=True)
        self.write_rss()
        self._save_seen()
        logger.info(f"[List] Initial poll done, {len(self.rss_items)} items loaded.")
        while True:
            sleep(LIST_INTERVAL)
            try:
                self.poll_once(first=False)
            except Exception as e:
                logger.exception(f"[List] Poll error: {e}")


# ============================================================
# 主入口
# ============================================================
@logger.catch()
def main():
    logger.add(level='DEBUG', sink=LOG_PATH, rotation='2 MB')

    magic = CatchMagic()
    magic.start_rss_http()

    tl = TorrentListMonitor()
    tl.start_rss_http()
    Thread(target=tl.run_loop, daemon=True, name="ListPoller").start()

    logger.info("All services started. Running CatchMagic loop in main thread...")
    while True:
        try:
            magic.run()
        except Exception as e:
            logger.error(f"[Magic] Run error: {e}")
        finally:
            gc.collect()
            sleep(INTERVAL)


if __name__ == '__main__':
    main()