import requests
import time
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# 配置参数
QB_URL = ""
QB_USER = ""
QB_PASS = ""
PASSKEY = ""
COMMON_SAVE_PATH = "./DouBan_IMDB.TOP250.Movies.Mixed.Collection.20240501.FRDS/"
FRDS_URL = ""

# 种子文件存储配置
TORRENT_CACHE_DIR = "./torrent_cache"  # 永久保存目录
TORRENT_TEMP_DIR = "./temp_torrents"    # 临时处理目录

# 最大并发下载数
MAX_WORKERS = 5

# 所有种子任务配置
TORRENT_JOBS = [
    {
        "save_path": COMMON_SAVE_PATH,
        "torrent_ids": [
            4446, 5887, 4236, 4909, 4274, 4340, 5819, 9539, 5558, 4503,
            5368, 5369, 10713, 4735, 1977231, 1976323, 5885, 9124, 7146, 6024,
            6140, 5764, 6405, 3855, 6616, 5992, 6030, 4186, 5985, 6456,
            6346, 3858, 5076, 2156530, 6291, 5883, 10914, 10802, 5187, 6998,
            4857, 915060, 4276, 5956, 5998, 5547, 5889, 5566, 8119, 5804,
            4669, 4668, 8544, 5975, 7065, 6639, 5303, 3980, 5748, 5749,
            3851, 4978, 6111, 5204, 6114, 6137, 10037, 6173, 4271, 5927,
            4114, 4447, 4417, 5460, 6143, 5729, 4568, 5993, 6087, 5066,
            10986, 9653, 2798, 1975815, 7893, 5032, 5813, 7346, 2165717, 7103,
            2196133, 6927, 5977, 5496, 3804, 6075, 6049, 4470, 13098, 5976,
            5089, 4848, 5321, 4239, 4618, 4544, 10352, 6747, 11256, 9317,
            6390, 10300, 5873, 6135, 6903, 6005, 5962, 2067929, 9290, 5892,
            4010, 6097, 5701, 5695, 6061, 7041, 5518, 1975635, 8511, 4495,
            5856, 10723, 6038, 6595, 10754, 7018, 6109, 5929, 5473, 5052,
            5095, 6099, 5860, 5772, 4018, 5406, 4418, 5452, 12228, 12101,
            5047, 9880, 6149, 5979, 1976170, 4752, 6043, 5562, 1974610, 4613,
            5951, 6073, 5068, 6094, 3841, 4444, 6514, 10652, 4234, 3849,
            5420, 12145, 6040, 6039, 6004, 5839, 5767, 4375, 5771, 4497,
            5952, 6358, 5260, 5984, 4326, 4554, 10994, 5070, 7356, 4403,
            4956, 4038, 5881, 5959, 2165718, 12442, 5945, 5704, 5960, 4959,
            6596, 5855, 11891, 9879, 5206, 4610, 4469, 7104, 6072, 10276,
            5087, 4895, 4943, 1974795, 1358430, 6045, 4437, 4391, 6721, 8779,
            5590, 6081, 7025, 6141, 2169398, 7159, 5114, 4106, 9863, 3847,
            5973, 2196149, 4377, 5794, 3933, 5859, 4606, 6163, 6112, 9580,
            5792, 5418, 6147, 10559, 5761, 5974, 6351, 13195, 10041, 6632,
            5775, 5046, 8780, 12793, 10720, 5067, 4037, 3965, 5380, 6115,
            5884, 5891, 7705, 5063, 10776, 11315, 6023, 10301, 6439, 12535,
            8247, 3854, 9301, 5765, 6330, 5851, 5858, 3967, 4122, 5850,
            6090, 6107, 7973, 10524, 4931, 4543, 4542, 9073, 5981, 6399,
            5886, 1491774, 12663, 4224, 6852, 9676, 3983, 5588, 5093, 5490,
            5312, 5375, 7190, 5931, 6132, 10860, 942516, 6108, 6134, 1975822,
            10987, 9126, 11295, 5834, 5835, 4523, 6389, 4218, 4118, 5487,
            5069, 2165716, 5130, 5936, 5870, 5077, 6215, 6438, 5882, 5997,
            7001, 3970, 6979, 7451, 6787, 3978, 5739, 5738, 9246, 4219, 6879
        ]
    },
    {
        "save_path": f"{COMMON_SAVE_PATH}蝙蝠侠前传合集.BluRay.1080p.x265.10bit.2Audio.MNHD-FRDS/",
        "torrent_ids": [3981, 3982, 3977]
    },
    {
        "save_path": f"{COMMON_SAVE_PATH}冰川时代_冰河世纪合集.1080p.x265.10bit.4Audio.MNHD-FRDS/",
        "torrent_ids": [10371, 10984, 10411, 10357, 10457]
    },
    {
        "save_path": f"{COMMON_SAVE_PATH}电锯惊魂合集.Saw.2004-2017.Collection.BluRay.1080p.x265.10bit.MNHD-FRDS/",
        "torrent_ids": [1977255, 1976046, 8600, 8605, 8620, 8628, 8639, 8643]
    },
    {
        "save_path": f"{COMMON_SAVE_PATH}谍影重重合集.BluRay.1080p.x265.10bit.2Audio.MNHD-FRDS/",
        "torrent_ids": [4536, 4537, 4540, 4539, 4866]
    },
    {
        "save_path": f"{COMMON_SAVE_PATH}夺宝奇兵合集.BluRay.1080p.x265.10bit.2Audio.MNHD-FRDS/",
        "torrent_ids": [5554, 5555, 5556, 5557, 1977284]
    },
    {
        "save_path": f"{COMMON_SAVE_PATH}疯狂的麦克斯合集.BluRay.1080p.x265.10bit.MNHD-FRDS/",
        "torrent_ids": [7637, 3971]
    },
    {
        "save_path": f"{COMMON_SAVE_PATH}复仇者联盟合集.BluRay.1080p.x265.10bit.MNHD-FRDS/",
        "torrent_ids": [4044, 4045, 7822, 9832]
    },
    {
        "save_path": f"{COMMON_SAVE_PATH}黑客帝国合集.BluRay.1080p.x265.10bit.2Audio.MNHD-FRDS/",
        "torrent_ids": [5127, 5128, 5132]
    },
    {
        "save_path": f"{COMMON_SAVE_PATH}加勒比海盗合集.BluRay.2160p.x265.10bit.DoVi.mUHD-FRDS/",
        "torrent_ids": [13626, 13615, 13620, 13624, 13656]
    },
    {
        "save_path": f"{COMMON_SAVE_PATH}玩具总动员合集.1080p.x265.10bit/",
        "torrent_ids": [5828, 5829, 5830, 10174]
    },
    {
        "save_path": f"{COMMON_SAVE_PATH}无间道三部曲.Infernal.Affairs.BluRay.1080p.x265.10bit.2Audio.MNHD-FRDS/",
        "torrent_ids": [4409, 4410, 4411]
    },
    {
        "save_path": f"{COMMON_SAVE_PATH}星球大战合集.BluRay.1080p.x265.10bit.2Audio.MNHD-FRDS/",
        "torrent_ids": [7580, 3807, 3808, 3809, 6492, 3810, 3811, 3812, 3902]
    },
    {
        "save_path": f"{COMMON_SAVE_PATH}蜘蛛侠合集.Spider.Man.BluRay.1080p.x265.10bit.MNHD-FRDS/",
        "torrent_ids": [8954, 294056, 1977138]
    },
    {
        "save_path": f"{COMMON_SAVE_PATH}指环王合集.BluRay.1080p.x265.10bit.2Audio.MNHD-FRDS/",
        "torrent_ids": [5151, 5143, 5144]
    },
    {
        "save_path": f"{COMMON_SAVE_PATH}终结者合集.BluRay.1080p.x265.10bit.MnHD-FRDS/",
        "torrent_ids": [4419, 4658, 6524, 6525, 4646, 10650]
    },
    {
        "save_path": f"{COMMON_SAVE_PATH}X战警合集.BluRay.1080p.x265.10bit/",
        "torrent_ids": [9974, 4628, 4109, 4110, 4111, 4061, 4066, 4112, 5660, 4113]
    },
    {
        "save_path": f"{COMMON_SAVE_PATH}壮志凌云合集2部.BluRay.1080p.x265.10bit/",
        "torrent_ids": [4235, 1975382]
    },
    {
        "save_path": f"{COMMON_SAVE_PATH}阿凡达.Avatar.2009.Extended.Collector's.Edition.Hybrid.BluRay.1080p.x265.10bit.DDP5.1.Repack.H.MNHD-FRDS/",
        "torrent_ids": [13658]
    },
    {
        "save_path": f"{COMMON_SAVE_PATH}我的父亲，我的儿子.Babam.ve.Oglum.2005.1080p.AMZN.WEB-DL.DDP5.1.H.264-KAIZEN/",
        "torrent_ids": [12599]
    },
    {
        "save_path": f"{COMMON_SAVE_PATH}背靠背，脸对脸.Back.to.Back.Face.to.Face.1994.Bluray.1080p.x265.10bit.FLAC.MNHD-FRDS/",
        "torrent_ids": [12202]
    },
    {
        "save_path": f"{COMMON_SAVE_PATH}请以你的名字呼唤我.Call.Me.by.Your.Name.2017.1080p.BluRay.x265.10bit.MNHD-FRDS/",
        "torrent_ids": [12782]
    },
    {
        "save_path": f"{COMMON_SAVE_PATH}自己去看.Come.And.See.1985.CC.Bluray.1080p.x265.10bit.FLAC.MNHD-FRDS/",
        "torrent_ids": [12383]
    },
    {
        "save_path": f"{COMMON_SAVE_PATH}超脱.Detachment.2011.Blu-Ray.1080p.AC3.x265.10bit-Yumi@FRDS/",
        "torrent_ids": [13374]
    },
    {
        "save_path": f"{COMMON_SAVE_PATH}一夜风流.It.Happened.One.Night.1934.CC.Bluray.1080p.x265.10bit.FLAC.MNHD-FRDS/",
        "torrent_ids": [11318]
    },
    {
        "save_path": f"{COMMON_SAVE_PATH}杰伊·比姆.Jai.Bhim.2021.2160p.AMZN.WEB-DL.HIN-TAM.DDP5.1.HDR.HEVC-Telly/",
        "torrent_ids": [1975853]
    },
    {
        "save_path": f"{COMMON_SAVE_PATH}克劳斯：圣诞节的秘密.Klaus.2019.Netflix.WEB-DL.1080p.HEVC.DDP-AREY/",
        "torrent_ids": [12600]
    },
    {
        "save_path": f"{COMMON_SAVE_PATH}哪吒闹海.Prince.Nezha's.Triumph.Against.Dragon.King.1979.Webrip.1080p.x265.10bit.AAC.MNHD-FRDS/",
        "torrent_ids": [11483]
    },
    {
        "save_path": f"{COMMON_SAVE_PATH}罗马假日.Roman.Holiday.1953.Bluray.1080p.x265.10bit.FLAC.MNHD-FRDS/",
        "torrent_ids": [11581]
    },
    {
        "save_path": f"{COMMON_SAVE_PATH}星球大战合集.BluRay.1080p.x265.10bit.2Audio.MNHD-FRDS/Star.Wars.Episode.IX.The.Rise.of.Skywalker.2019.UHD.BluRay.1080p.x265.10bit.DDP.7.1.MNHDR-FRDS/",
        "torrent_ids": [12023]
    },
    {
        "save_path": f"{COMMON_SAVE_PATH}射雕英雄传之东成西就.The.Eagle.Shooting.Heroes.1993.1080p.KOREA.BluRay.x265.10bit.DD+5.1.MNHD-FRDS/",
        "torrent_ids": [12862]
    }
]


def log(msg, level="INFO"):
    """统一日志输出"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level}] {msg}")


def init_dirs():
    """初始化目录"""
    Path(TORRENT_CACHE_DIR).mkdir(parents=True, exist_ok=True)
    Path(TORRENT_TEMP_DIR).mkdir(parents=True, exist_ok=True)
    log(f"缓存目录已创建: {TORRENT_CACHE_DIR}")
    log(f"临时目录已创建: {TORRENT_TEMP_DIR}")


def get_local_torrent(torrent_id):
    """检查本地是否存在对应ID的种子文件"""
    torrent_path = os.path.join(TORRENT_CACHE_DIR, f"{torrent_id}.torrent")
    if os.path.exists(torrent_path):
        # 验证文件大小（种子文件通常不会为空）
        if os.path.getsize(torrent_path) > 0:
            return torrent_path
    return None


def download_torrent(torrent_id, session, current, total):
    """下载单个种子文件或从本地获取"""
    # 先检查本地缓存
    local_path = get_local_torrent(torrent_id)
    if local_path:
        log(f"[{current}/{total}] ✓ 使用本地缓存: {torrent_id}", "CACHE")
        return local_path

    # 本地没有则从网络下载
    url = f"{FRDS_URL}download.php?id={torrent_id}&passkey={PASSKEY}&https=1"
    cache_path = os.path.join(TORRENT_CACHE_DIR, f"{torrent_id}.torrent")
    temp_path = os.path.join(TORRENT_TEMP_DIR, f"{torrent_id}.torrent")

    try:
        log(f"[{current}/{total}] 正在下载种子 ID: {torrent_id}")
        resp = session.get(url, timeout=30)
        if resp.status_code == 200:
            # 保存到缓存目录
            with open(cache_path, 'wb') as f:
                f.write(resp.content)
            # 复制到临时目录供添加使用
            with open(temp_path, 'wb') as f:
                f.write(resp.content)
            log(f"[{current}/{total}] ✓ 种子下载成功: {torrent_id}", "SUCCESS")
            return temp_path
        else:
            log(f"[{current}/{total}] ✗ 种子下载失败 {torrent_id}: HTTP {resp.status_code}", "ERROR")
            log(f"    失败链接: {url}", "ERROR")
            log(f"    错误原因: HTTP状态码 {resp.status_code} - {resp.reason}", "ERROR")
            return None
    except requests.exceptions.Timeout:
        log(f"[{current}/{total}] ✗ 种子下载超时 {torrent_id}", "ERROR")
        log(f"    失败链接: {url}", "ERROR")
        log(f"    错误原因: 请求超时 (30秒)", "ERROR")
        return None
    except requests.exceptions.ConnectionError as e:
        log(f"[{current}/{total}] ✗ 种子下载连接错误 {torrent_id}", "ERROR")
        log(f"    失败链接: {url}", "ERROR")
        log(f"    错误原因: {str(e)}", "ERROR")
        return None
    except Exception as e:
        log(f"[{current}/{total}] ✗ 种子下载异常 {torrent_id}", "ERROR")
        log(f"    失败链接: {url}", "ERROR")
        log(f"    错误原因: {type(e).__name__} - {str(e)}", "ERROR")
        return None


def download_torrents_batch(torrent_ids):
    """批量并发下载或获取种子文件"""
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0'})

    downloaded_paths = []
    total = len(torrent_ids)

    # 统计本地缓存中已有的文件
    cached_count = sum(1 for tid in torrent_ids if get_local_torrent(tid))
    log(f"开始处理 {total} 个种子 (本地缓存: {cached_count}, 需下载: {total - cached_count}, 并发数: {MAX_WORKERS})")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_id = {}
        for idx, tid in enumerate(torrent_ids, 1):
            future = executor.submit(download_torrent, tid, session, idx, total)
            future_to_id[future] = tid

        completed = 0
        for future in as_completed(future_to_id):
            completed += 1
            path = future.result()
            if path:
                downloaded_paths.append(path)

            # 进度统计
            success_count = len(downloaded_paths)
            fail_count = completed - success_count
            log(f"处理进度: {completed}/{total} | 成功: {success_count} | 失败: {fail_count}", "PROGRESS")

    log(f"批量处理完成: 成功 {len(downloaded_paths)}/{total}", "SUCCESS")
    return downloaded_paths


def qb_login():
    """登录qBittorrent"""
    log("正在登录 qBittorrent...")
    session = requests.Session()
    resp = session.post(
        f"{QB_URL}/api/v2/auth/login",
        data={"username": QB_USER, "password": QB_PASS}
    )

    if "Fails." in resp.text:
        log("qBittorrent登录失败，请检查用户名密码", "ERROR")
        raise Exception("qBittorrent登录失败")

    log("qBittorrent登录成功", "SUCCESS")
    return session


def qb_add_torrent_files(session, torrent_paths, save_path):
    """批量添加种子文件到qBittorrent"""
    log(f"正在添加 {len(torrent_paths)} 个种子到 qBittorrent...")

    files = []
    for path in torrent_paths:
        try:
            files.append(('torrents', open(path, 'rb')))
        except Exception as e:
            log(f"打开种子文件失败 {path}: {e}", "ERROR")

    if not files:
        log("没有可用的种子文件", "ERROR")
        return False

    try:
        payload = {
            "savepath": save_path,
            "paused": "true",
            "skip_checking": "true",
            "autoTMM": "false"
        }

        resp = session.post(
            f"{QB_URL}/api/v2/torrents/add",
            data=payload,
            files=files,
            headers={"Referer": QB_URL}
        )

        if resp.status_code == 200:
            log(f"成功添加 {len(files)} 个种子到 qBittorrent", "SUCCESS")
            return True
        else:
            log(f"添加种子失败: HTTP {resp.status_code}", "ERROR")
            return False
    finally:
        # 关闭所有文件句柄
        for _, file_obj in files:
            file_obj.close()


def cleanup_temp_files(torrent_paths):
    """清理临时种子文件（只清理临时目录，保留缓存）"""
    log(f"正在清理 {len(torrent_paths)} 个临时种子文件...")
    cleaned = 0
    for path in torrent_paths:
        # 只删除临时目录中的文件
        if TORRENT_TEMP_DIR in path:
            try:
                os.remove(path)
                cleaned += 1
            except Exception as e:
                log(f"清理文件失败 {path}: {e}", "WARN")
    log(f"临时文件清理完成: {cleaned}/{len(torrent_paths)}")


def get_cache_stats():
    """获取缓存统计信息"""
    if not os.path.exists(TORRENT_CACHE_DIR):
        return 0, 0
    
    files = [f for f in os.listdir(TORRENT_CACHE_DIR) if f.endswith('.torrent')]
    total_size = sum(os.path.getsize(os.path.join(TORRENT_CACHE_DIR, f)) for f in files)
    return len(files), total_size / (1024 * 1024)  # 转换为MB


def main():
    start_time = time.time()

    print("\n" + "=" * 80)
    print(" " * 20 + "qBittorrent 辅种跳检脚本 v3.0 (带缓存)")
    print("=" * 80 + "\n")

    # 统计信息
    total_jobs = len(TORRENT_JOBS)
    total_torrents = sum(len(job["torrent_ids"]) for job in TORRENT_JOBS)
    cache_count, cache_size = get_cache_stats()
    
    log(f"任务统计: 共 {total_jobs} 个任务 | 共 {total_torrents} 个种子")
    log(f"缓存统计: 已缓存 {cache_count} 个种子 | 缓存大小 {cache_size:.2f} MB")

    # 初始化
    init_dirs()

    try:
        qb_session = qb_login()

        success_jobs = 0
        success_torrents = 0
        total_cached_hits = 0

        for idx, job in enumerate(TORRENT_JOBS, 1):
            save_path = job["save_path"]
            torrent_ids = job["torrent_ids"]

            print("\n" + "-" * 80)
            log(f"任务 [{idx}/{total_jobs}] 开始")
            log(f"保存路径: {save_path}")
            log(f"种子数量: {len(torrent_ids)}")
            print("-" * 80)

            # 批量处理种子文件（优先使用本地缓存）
            torrent_paths = download_torrents_batch(torrent_ids)

            if not torrent_paths:
                log(f"任务 [{idx}/{total_jobs}] 失败: 没有可用的种子文件", "ERROR")
                continue

            # 添加到qBittorrent
            if qb_add_torrent_files(qb_session, torrent_paths, save_path):
                success_jobs += 1
                success_torrents += len(torrent_paths)
                total_cached_hits += sum(1 for p in torrent_paths if TORRENT_TEMP_DIR in p)
                log(f"任务 [{idx}/{total_jobs}] 完成", "SUCCESS")
            else:
                log(f"任务 [{idx}/{total_jobs}] 失败: 无法添加到qBittorrent", "ERROR")

            # 清理临时文件
            cleanup_temp_files(torrent_paths)

            # 任务间延迟
            if idx < total_jobs:
                time.sleep(1)

        # 最终统计
        elapsed_time = time.time() - start_time
        cache_count, cache_size = get_cache_stats()
        
        print("\n" + "=" * 80)
        log("所有任务执行完成！", "SUCCESS")
        print("=" * 80)
        log(f"成功任务: {success_jobs}/{total_jobs}")
        log(f"成功种子: {success_torrents}/{total_torrents}")
        log(f"总耗时: {elapsed_time:.2f} 秒")
        log(f"最终缓存: {cache_count} 个种子 | {cache_size:.2f} MB")
        print("=" * 80 + "\n")

    except Exception as e:
        log(f"程序异常: {str(e)}", "ERROR")
        import traceback
        traceback.print_exc()
    finally:
        # 清理临时目录（保留缓存目录）
        try:
            if os.path.exists(TORRENT_TEMP_DIR):
                for file in os.listdir(TORRENT_TEMP_DIR):
                    os.remove(os.path.join(TORRENT_TEMP_DIR, file))
                os.rmdir(TORRENT_TEMP_DIR)
                log("临时目录已清理")
        except Exception as e:
            log(f"清理临时目录失败: {e}", "WARN")


if __name__ == "__main__":
    main()