# -*- coding: utf-8 -*-
"""
素材坞 · 全机素材索引
只读扫描磁盘上的图片 / 视频 / 音频 / 文档，建索引并起一个本地网页，支持分类、搜索、缩略图预览、直接打开原文件。
扫描和浏览不复制、不移动任何用户文件；删除、重命名、批量整理只在你明确确认后执行，删除进入软件待删除箱。

用法：
  python 库服务.py            有索引就开界面，没有就先扫描
  python 库服务.py scan       只扫描（增量）
  python 库服务.py scan --full 全量重扫
  python 库服务.py serve      只开界面
"""
import os
import re
import sys
import io
import json
import time
import math
import uuid
import shutil
import socket
import ssl
import base64
import hashlib
import glob
import sqlite3
import tempfile
import subprocess
import webbrowser
import threading
import urllib.request
import urllib.error
import urllib.robotparser
import ipaddress
import html as htmllib
try:
    import certifi
    _CA_BUNDLE = certifi.where()
except Exception:
    _CA_BUNDLE = ''
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote, urlencode, urljoin

try:
    import imagesize  # 只读文件头拿宽高，比 Pillow 解码快很多；装不上就回退 Pillow
except Exception:
    imagesize = None

if getattr(sys, 'frozen', False):
    # 打包成 exe 后，数据目录跟着 exe 所在文件夹走（便携式，拷到哪台机器都能用）
    BASE = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IDX = os.path.join(BASE, '00_索引')
DB = os.path.join(IDX, 'library.db')
THUMB_DIR = os.path.join(IDX, '缩略图')
PREVIEW_DIR = os.path.join(IDX, '预览缓存')
PLAY_DIR = os.path.join(IDX, '播放缓存')
TRASH_DIR = os.path.join(IDX, '待删除')
TOOLS = os.path.join(BASE, '_工具')
APP_HTML = os.path.join(TOOLS, 'app.html')

# v1.7：资源搜索的下载隔离区与预览缓存。所有网络文件先落隔离区，校验/扫描通过后才允许入库。
DISCOVER_DIR = os.path.join(IDX, '资源下载')
DISCOVER_QUAR = os.path.join(DISCOVER_DIR, '_隔离')
DISCOVER_THUMB = os.path.join(IDX, '资源预览')
DISCOVER_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
               '(KHTML, like Gecko) Chrome/124.0 Safari/537.36 AssetDock/1.7.0')
DISCOVER_MAX_DL = 200 * 1024 * 1024
DISCOVER_MAX_HTML = 6 * 1024 * 1024
DISCOVER_MAX_THUMB = 14 * 1024 * 1024
DISCOVER_JOB = {'running': False, 'phase': 'idle', 'msg': '', 'progress': 0, 'result': None,
                'error': '', 'started': 0}
DISCOVER_LOCK = threading.Lock()
ALLOW_PRIVATE = os.environ.get('ASSETDOCK_ALLOW_PRIVATE') == '1'

# 统计缓存：任何索引/标签/收藏变化都让版本号 +1，旧缓存立即失效
_DATA_VER = 0
_STATS_CACHE = {}
_DUP_CACHE = {}
_CACHE_LOCK = threading.Lock()
TRASH_DAYS = 7


def bump_data():
    """数据发生变化后调用，下一次请求重新算统计"""
    global _DATA_VER
    with _CACHE_LOCK:
        _DATA_VER += 1
        _STATS_CACHE.clear()
        _DUP_CACHE.clear()


def app_html_file():
    """界面文件：优先用旁边的 _工具\app.html（方便随时改），没有就用打包进 exe 的那份"""
    if os.path.exists(APP_HTML):
        return APP_HTML
    mei = getattr(sys, '_MEIPASS', '')
    if mei:
        p = os.path.join(mei, 'app.html')
        if os.path.exists(p):
            return p
    return APP_HTML

IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.tif', '.tiff', '.svg', '.heic',
             '.heif', '.avif', '.psd', '.ico', '.jxl'}
VIDEO_EXT = {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v', '.flv', '.wmv', '.mpg', '.mpeg', '.ts'}
AUDIO_EXT = {'.mp3', '.wav', '.m4a', '.ogg', '.flac', '.aac', '.wma', '.opus'}
DOC_EXT = {'.pdf', '.ppt', '.pptx'}
# 浏览器内核能直接播的容器；其余（mov/mkv/avi/flv/wmv/mpg/ts）先转成 mp4
DIRECT_VIDEO = {'.mp4', '.m4v', '.webm'}

DEFAULT_SKIP = [
    '\\windows', '\\program files', '\\program files (x86)', '\\programdata',
    '\\$recycle.bin', '\\system volume information', '\\appdata\\', '\\node_modules\\',
    '\\.git\\', '\\__pycache__\\', '\\site-packages\\', '\\temp\\', '\\_iso\\',
    '\\.workbuddy\\', '\\.bak\\', '\\缩略图\\', '\\doubao\\user data\\', '\\wechat files\\',
    '\\tencent files\\', '\\cache\\', '\\cache2\\', '\\sandbox_runtime\\',
    '\\00_索引\\', '\\_工具\\py\\',
    # 常见软件的素材缓存，不是用户自己的东西
    '\\wx\\', '\\wps office\\', '\\jianyingpro\\', '\\adobe\\', '\\dingtalk\\',
    '\\dingding\\', '\\trae cn\\', '\\unity\\', '\\unityinstall\\', '\\godot\\',
    '\\deepseek\\', '\\corelibs\\', '\\zihun', '\\workbuddy\\',
    # 云盘/同步盘：目录里是占位文件，遍历或 stat 会触发客户端下载
    '\\wpsdrive\\', '\\wps cloud\\', '\\wps cloud files\\', '\\wps云盘\\',
    '\\onedrive', '\\dropbox', '\\google drive', '\\icloud drive',
    '\\baidunetdisk', '\\nutstore', '\\aliyundrive',
    # 工具/调试残留
    '\\app.asar.unpacked\\', '\\.vscode\\', '\\.cdp_open\\', '\\.shots\\',
    '\\node_modules\\', '\\.git\\',
]
DEFAULT_ROOTS = ['D:\\', 'E:\\', os.path.expanduser('~')]


def default_roots():
    """新电脑上默认扫：所有本地固定盘（C 盘只收用户目录，避免系统文件）+ 用户目录"""
    drives = []
    try:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            if mask & (1 << i):
                letter = '%s:\\' % chr(65 + i)
                if ctypes.windll.kernel32.GetDriveTypeW(letter) == 3:   # 3 = 本地固定盘
                    drives.append(letter)
    except Exception:
        drives = ['C:\\', 'D:\\', 'E:\\']
    home = os.path.expanduser('~')
    roots = []
    seen = set()
    for d in drives:
        target = home if d.lower() == 'c:\\' else d
        if target.lower() not in seen:
            seen.add(target.lower())
            roots.append(target)
    if home.lower() not in seen:
        roots.insert(0, home)
    return roots

# 分类规则：自上而下第一个命中的生效
CAT_RULES = [
    ('系统与缓存', ('\\appdata\\', '\\windows\\', '\\programdata\\', '\\node_modules\\',
                    '\\site-packages\\', '\\__pycache__\\', '\\temp\\', '\\cache')),
    ('游戏素材', ('\\小游戏\\', '\\气球\\', '\\圣火喵\\', '\\玄黄喵\\', '\\上善若水\\',
                  '\\balloon', '\\game\\art\\', '\\sprites', '\\tileset')),
    ('证照文档', ('\\资质', '\\证书', '\\发票', '\\合同', '\\报表', '\\简历', '\\备案',
                  '\\营业执照', '\\检验报告', '\\documents\\', '\\wpsdrive\\', '\\文档\\')),
    ('产品物料', ('\\产品画册', '\\产品图片', '\\详情页', '\\主图', '\\包装', '\\商品图',
                  '\\画册', '\\sku', '\\平面素材', '\\产品图')),
    ('品牌宣传', ('\\官网', '\\宣传', '\\公众号', '\\推广', '\\banner', '\\海报素材',
                  '\\朋友圈', '\\推文')),
    ('茶饮工作', ('\\顺然素材\\', '\\烘焙素材\\', '\\茶饮', '\\巢铺\\', '\\卡旺卡\\',
                  '\\产品应用', '\\配方')),
    ('电商视频', ('\\电商视频\\', '\\视频成片\\', '\\成片\\')),
    ('截图', ('\\截图\\', '\\screenshots\\', '\\screen shot', '\\screenshot', '\\验收截图')),
    ('聊天图片', ('\\微信图片', '\\mmexport', '\\qq图片', '\\wechat image')),
    ('实拍照片', ('\\照片\\', '\\相片', '\\摄影\\', '\\实拍', '\\dcim\\', '\\camera\\', '\\相机')),
    ('表情包', ('\\表情', 'sticker', '\\贴纸', '\\九宫格', '\\合图', 'emoji')),
    ('壁纸', ('\\壁纸\\', '\\wallpaper', '\\backgrounds')),
    ('设计稿', ('\\设计', '\\主视觉', '\\封面', '\\logo', '\\poster', '\\kv')),
    ('下载', ('\\downloads\\', '\\下载\\', '\\桌面\\', '\\desktop\\')),
    ('素材整理', ('\\素材\\', '\\图集\\', '\\参考\\', '\\美术\\', '\\art\\')),
]


def media_of(ext):
    e = ext.lower()
    if e in IMAGE_EXT:
        return '图片'
    if e in VIDEO_EXT:
        return '视频'
    if e in AUDIO_EXT:
        return '音频'
    if e in DOC_EXT:
        return '文档'
    return None


def guess_cat(path, media):
    low = path.lower()
    for cat, hints in CAT_RULES:
        for h in hints:
            if h in low:
                return cat
    if media == '视频':
        return '视频'
    if media == '文档':
        return '文档'
    return '其他'


PROJECTS = [
    ('气球冲冲冲', ('\\气球\\', '\\balloon')),
    ('圣火喵', ('\\圣火喵\\',)),
    ('玄黄喵', ('\\玄黄喵\\',)),
    ('上善若水', ('\\上善若水\\',)),
]
ING_PREFIX = [('t_', '茶叶'), ('s_', '糖蜜'), ('f_', '水果'), ('p_', '辅料'), ('c_', '咖啡'), ('m_', '牛乳')]


def sub_of(path, media):
    """在分类下面再细分一层：项目内按部件/道具/界面分，视频按用途分"""
    low = path.lower()
    name = os.path.basename(low)

    if media == '视频':
        if '录屏' in low or 'screen' in low or 'recording' in low:
            return '录屏'
        if '宣传' in low or '广告' in low or 'tvc' in low:
            return '宣传片'
        if '动画' in low or 'anim' in low:
            return '动画'
        if '微信' in low or 'mmexport' in low:
            return '微信视频'
        return ''

    proj = ''
    tail = low
    for label, hints in PROJECTS:
        for h in hints:
            i = low.find(h)
            if i >= 0:
                proj = label
                tail = low[i + len(h):]  # 项目根目录之后的相对路径，避免根目录名本身占掉细分
                break
        if proj:
            break
    if not proj:
        return ''

    if proj == '玄黄喵':
        if 'ingredients' in tail:
            for pre, label in ING_PREFIX:
                if name.startswith(pre):
                    return label
            return '原料'
        for key, label in (('guests', '客人'), ('cup_parts', '杯具'), ('props', '道具'),
                           ('interior', '场景'), ('scene', '场景'), ('\\ui\\', '界面')):
            if '\\' + key + '\\' in tail:
                return label
        if 'gpt' in tail:
            return 'AI生图'
        return ''

    if proj == '气球冲冲冲':
        for key, label in (('背景', '背景'), ('平台', '平台'), ('装饰', '装饰'), ('道具', '道具'),
                           ('障碍', '障碍'), ('皮肤', '皮肤'), ('气球', '气球'), ('\\ui\\', '界面')):
            if '\\' + key + '\\' in tail:
                return label
        if 'ball_pull' in name:
            return '拉拽帧'
        if 'ball_soft' in name or 'balloon_soft' in name:
            return '变形帧'
        if name.startswith('fx_') or 'airflow' in tail:
            return '特效'
        if 'gpt_image' in tail or 'generated' in tail:
            return 'AI生图'
        return ''

    if proj == '圣火喵':
        if 'concepts' in tail:
            return '原画'
        if '\\gpt\\' in tail or '\\gpt' in tail:
            return 'AI生图'
        if 'pick-icons' in tail or name.startswith('icon') or 'icon_' in name:
            return '图标'
        if 'pick-frames' in tail:
            return '截图'
        if '表情' in name:
            return '表情'
        if any(k in tail for k in ('cur_mon', 'mon_clean', 'f_mon', '\\mon')):
            return '怪物帧'
        if 'current_cat' in tail:
            return '主角帧'
        if any(k in tail for k in ('f_claw', 'f_rush', 'f_atk', '\\rush')):
            return '动作帧'
        if any(k in tail for k in ('fx', 'atk', 'claw', 'hit')):
            return '特效'
        return ''

    if proj == '上善若水':
        if '设定图' in tail:
            return '设定图'
        if '像素' in tail or '\\m9\\' in tail or '\\m10\\' in tail:
            return '像素稿'
        if name.startswith('gpt_act'):
            return '动作稿'
        for pre, label in (('p_', '主角'), ('spr_', '生灵'), ('icon_', '图标'), ('fg_', '前景')):
            if name.startswith(pre):
                return label
        if any(name.startswith(p) for p in ('hut_', 'mountain_', 'bg_', 'env_', 'strip_')):
            return '场景'
        return ''
    return ''


def location_of(path):
    d = os.path.dirname(path)
    parts = d.split(os.sep)
    if len(parts) >= 4 and parts[0].upper().startswith('C:') and parts[1].lower() == 'users':
        return os.sep.join(parts[:4])
    if len(parts) >= 2:
        return parts[0] + os.sep + parts[1]
    return d


def load_list(name, default):
    p = os.path.join(TOOLS, name)
    if not os.path.exists(p):
        return default
    out = []
    with open(p, 'r', encoding='utf-8-sig') as f:
        for line in f:
            s = line.strip()
            if s and not s.startswith('#'):
                out.append(s)
    return out or default


def ensure_dirs():
    for d in (IDX, THUMB_DIR, DISCOVER_DIR, DISCOVER_QUAR, DISCOVER_THUMB):
        os.makedirs(d, exist_ok=True)
    _clean_discover_quarantine()


def connect():
    con = sqlite3.connect(DB, timeout=30)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA synchronous=NORMAL')
    con.execute('PRAGMA temp_store=MEMORY')
    con.execute('PRAGMA cache_size=-65536')
    con.execute('PRAGMA mmap_size=268435456')
    global _MIGRATED
    if not _MIGRATED:
        con.executescript(SCHEMA)
        cols = [r[1] for r in con.execute('PRAGMA table_info(items)')]
        extra = {
            'sub': "TEXT DEFAULT ''",
            'sha': "TEXT DEFAULT ''",
            'phash': "TEXT DEFAULT ''",
            'alpha': 'INTEGER DEFAULT 0',
            'dur': 'REAL DEFAULT 0',
            'content': "TEXT DEFAULT ''",
            'content_state': 'INTEGER DEFAULT 0',
            'hash_mtime': 'INTEGER DEFAULT 0',
            'content_mtime': 'INTEGER DEFAULT 0',
            'meta_state': 'INTEGER DEFAULT 0',
        }
        for name, ddl in extra.items():
            if name not in cols:
                con.execute('ALTER TABLE items ADD COLUMN %s %s' % (name, ddl))
        con.execute('CREATE INDEX IF NOT EXISTS idx_sub ON items(sub)')
        con.execute('CREATE INDEX IF NOT EXISTS idx_sha ON items(sha)')
        con.execute('CREATE INDEX IF NOT EXISTS idx_size ON items(size)')
        con.execute('CREATE INDEX IF NOT EXISTS idx_meta_state ON items(media, meta_state)')
        con.commit()
        _MIGRATED = True
    return con


_MIGRATED = False


SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  ext TEXT,
  media TEXT,
  size INTEGER,
  mtime INTEGER,
  loc TEXT,
  cat TEXT,
  sub TEXT DEFAULT '',
  w INTEGER,
  h INTEGER,
  sha TEXT DEFAULT '',
  phash TEXT DEFAULT '',
  alpha INTEGER DEFAULT 0,
  dur REAL DEFAULT 0,
  content TEXT DEFAULT '',
  content_state INTEGER DEFAULT 0,
  hash_mtime INTEGER DEFAULT 0,
  content_mtime INTEGER DEFAULT 0,
  meta_state INTEGER DEFAULT 0,
  seen_run INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_media ON items(media);
CREATE INDEX IF NOT EXISTS idx_cat ON items(cat);
CREATE INDEX IF NOT EXISTS idx_loc ON items(loc);
CREATE INDEX IF NOT EXISTS idx_name ON items(name);
CREATE INDEX IF NOT EXISTS idx_size ON items(size);

CREATE TABLE IF NOT EXISTS tags(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  color TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS item_tags(
  item_id INTEGER NOT NULL,
  tag_id INTEGER NOT NULL,
  PRIMARY KEY(item_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_item_tags_tag ON item_tags(tag_id);
CREATE TABLE IF NOT EXISTS fav(
  item_id INTEGER PRIMARY KEY,
  created_at INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS trash(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER,
  orig_path TEXT NOT NULL,
  trash_path TEXT NOT NULL,
  name TEXT NOT NULL,
  ext TEXT DEFAULT '',
  media TEXT DEFAULT '',
  size INTEGER DEFAULT 0,
  mtime INTEGER DEFAULT 0,
  deleted_at INTEGER DEFAULT 0,
  expire_at INTEGER DEFAULT 0,
  batch TEXT DEFAULT '',
  tags TEXT DEFAULT '',
  fav INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_trash_batch ON trash(batch);
"""


PROGRESS = {'phase': 'idle', 'scanned': 0, 'roots': 0, 'ri': 0, 'root': '',
            't0': time.time(), 'total_db': 0, 'msg': ''}
PROG_LOCK = threading.Lock()


def set_progress(**kw):
    with PROG_LOCK:
        PROGRESS.update(kw)


def get_progress():
    with PROG_LOCK:
        d = dict(PROGRESS)
    d['elapsed'] = round(time.time() - d.get('t0', time.time()), 1)
    return d


def is_cloud_placeholder(st):
    """Windows 云盘占位文件（离线/按需下载）不要 stat 后继续读，否则会触发下载。"""
    attrs = getattr(st, 'st_file_attributes', 0) or 0
    return bool(attrs & (0x1000 | 0x40000 | 0x400000))


def do_scan(full=False):
    ensure_dirs()
    roots = load_list('扫描范围.txt', default_roots())
    skip = load_list('跳过目录.txt', DEFAULT_SKIP)
    skip = [s.lower() for s in skip]
    # 程序自己所在的文件夹永远不进索引（否则会把索引库、缩略图、界面预览图收进来）
    skip.append(os.path.abspath(BASE).lower() + os.sep)

    con = connect()
    con.executescript(SCHEMA)
    cols = [r[1] for r in con.execute('PRAGMA table_info(items)')]
    if 'sub' not in cols:
        con.execute("ALTER TABLE items ADD COLUMN sub TEXT DEFAULT ''")
        con.commit()
    con.execute('CREATE INDEX IF NOT EXISTS idx_sub ON items(sub)')
    if full:
        con.execute('DELETE FROM items')
        con.commit()

    run_id = int(time.time() * 1000)
    old = {}
    for pid, path, size, mtime in con.execute('SELECT id, path, size, mtime FROM items'):
        old[path] = (pid, size, mtime)

    print('扫描范围：')
    for r in roots:
        print('  ' + r)
    print('命中文件会写入索引，跳过规则 %d 条' % len(skip))

    added = updated = scanned = 0
    seen = set()
    batch = []
    t0 = time.time()
    set_progress(phase='scan', scanned=0, roots=len(roots), ri=0, root='',
                 t0=t0, total_db=con.execute('SELECT COUNT(*) FROM items').fetchone()[0],
                 msg='正在遍历磁盘')

    def flush():
        nonlocal added, updated
        if not batch:
            return
        con.executemany(
            'INSERT INTO items(path,name,ext,media,size,mtime,loc,cat,sub,seen_run) '
            'VALUES(?,?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(path) DO UPDATE SET name=excluded.name, ext=excluded.ext, media=excluded.media, '
            'size=excluded.size, mtime=excluded.mtime, loc=excluded.loc, cat=excluded.cat, '
            'sub=excluded.sub, seen_run=excluded.seen_run, '
            'w=CASE WHEN items.mtime<>excluded.mtime OR items.size<>excluded.size THEN NULL ELSE items.w END, '
            'h=CASE WHEN items.mtime<>excluded.mtime OR items.size<>excluded.size THEN NULL ELSE items.h END, '
            'alpha=CASE WHEN items.mtime<>excluded.mtime OR items.size<>excluded.size THEN 0 ELSE items.alpha END, '
            'dur=CASE WHEN items.mtime<>excluded.mtime OR items.size<>excluded.size THEN 0 ELSE items.dur END, '
            'meta_state=CASE WHEN items.mtime<>excluded.mtime OR items.size<>excluded.size THEN 0 ELSE items.meta_state END',
            batch)
        con.commit()
        batch.clear()

    for ri, root in enumerate(roots, 1):
        root_low = os.path.abspath(root).lower().rstrip('\\') + '\\'
        if any(s in root_low for s in skip):
            print('  跳过（云盘/规则命中）：' + root)
            continue
        if not os.path.isdir(root):
            print('  跳过（不存在）：' + root)
            continue
        set_progress(ri=ri, root=root, msg='正在扫描 ' + root)
        for dirpath, dirnames, filenames in os.walk(root):
            low = dirpath.lower().rstrip('\\') + '\\'
            dirnames[:] = [d for d in dirnames
                           if not any(s.strip('\\') == d.lower() for s in skip)
                           and not any(s in (low + d.lower() + '\\') for s in skip)]
            for fn in filenames:
                if fn.startswith('~$') or fn.startswith('._'):
                    continue  # Office 临时锁文件 / macOS 资源叉，不是真文档
                ext = os.path.splitext(fn)[1]
                media = media_of(ext)
                if not media:
                    continue
                fpath = os.path.join(dirpath, fn)
                try:
                    st = os.stat(fpath)
                except OSError:
                    continue
                if is_cloud_placeholder(st):
                    continue
                if st.st_size <= 0:
                    continue
                # .ts 既是视频流也是 TypeScript 源码：真视频首字节是 0x47 同步字节，否则当代码跳过
                if ext.lower() == '.ts':
                    try:
                        with open(fpath, 'rb') as fh:
                            if fh.read(1) != b'\x47':
                                continue
                    except OSError:
                        continue
                scanned += 1
                if scanned % 400 == 0:
                    set_progress(scanned=scanned, msg=dirpath)
                rec = old.get(fpath)
                changed = (rec is None or rec[1] != st.st_size or rec[2] != int(st.st_mtime))
                batch.append((fpath, fn, ext.lower(), media, st.st_size, int(st.st_mtime),
                              location_of(fpath), guess_cat(fpath, media), sub_of(fpath, media), run_id))
                if changed:
                    if rec is None:
                        added += 1
                    else:
                        updated += 1
                if len(batch) >= 500:
                    flush()
            if scanned and scanned % 5000 == 0:
                print('  已处理 %d 个文件… 用时 %.0f 秒' % (scanned, time.time() - t0))

    flush()
    # 清理已不存在的条目（只清理本次覆盖范围内的）
    if not full:
        maxrun = con.execute('SELECT MAX(seen_run) FROM items').fetchone()[0] or 0
        cur = con.execute('SELECT id, path FROM items WHERE seen_run<>?', (maxrun,)).fetchall()
        gone = [(i,) for i, p in cur if any(p.lower().startswith(r.lower()) for r in roots)]
        if gone:
            con.executemany('DELETE FROM items WHERE id=?', gone)
            con.commit()
            print('清理失效记录 %d 条（已不在扫描范围或源文件已删）' % len(gone))
    con.execute('CREATE INDEX IF NOT EXISTS idx_cat2 ON items(cat, media)')
    con.commit()
    total_now = con.execute('SELECT COUNT(*) FROM items').fetchone()[0]
    con.close()
    bump_data()
    set_progress(phase='ready', scanned=scanned, total_db=total_now, msg='索引完成')
    print('扫描完成：命中 %d 个媒体文件，新增 %d，更新 %d，用时 %.0f 秒' % (scanned, added, updated, time.time() - t0))
    try:
        start_meta_index()
    except Exception:
        pass


# ---------------- 缩略图 ----------------
_thumb_lock = threading.Lock()

# 浏览器本身能显示的图片格式，直接走原文件；其余在服务端转成 PNG 再给查看器
BROWSER_IMG = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.bmp', '.avif', '.ico'}


def preview_for(item_id, path, mtime):
    """把 tif/tiff/psd/heic/heif/jxl 这类转成 PNG 缓存，供内置查看器显示"""
    key = '%d_%d.png' % (item_id, mtime)
    out = os.path.join(PREVIEW_DIR, key[:2], key)
    if os.path.exists(out):
        return out
    try:
        if os.path.getsize(path) > 150 * 1024 * 1024:
            return None
        from PIL import Image, ImageOps
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except Exception:
            pass
        with _thumb_lock:
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im)
                if im.mode not in ('RGB', 'RGBA', 'L'):
                    im = im.convert('RGBA')
                im.thumbnail((4096, 4096), Image.LANCZOS)
                os.makedirs(os.path.dirname(out), exist_ok=True)
                im.save(out, 'PNG', optimize=True)
        return out
    except Exception:
        return None


def placeholder_thumb(kind, ext):
    """给视频/音频/无法解码的图片生成一张占位封面，避免界面上出现破图"""
    from PIL import Image, ImageDraw
    out = os.path.join(THUMB_DIR, '_place', '%s_%s.webp' % (kind, (ext or 'x').lstrip('.')))
    if os.path.exists(out):
        return out
    os.makedirs(os.path.dirname(out), exist_ok=True)
    im = Image.new('RGB', (360, 360), (18, 22, 30))
    d = ImageDraw.Draw(im)
    if kind == '视频':
        d.polygon([(150, 130), (150, 230), (235, 180)], fill=(120, 130, 255))
        d.text((150, 250), 'VIDEO', fill=(140, 148, 170))
    elif kind == '音频':
        for i, h in enumerate([60, 110, 150, 110, 60]):
            x = 120 + i * 26
            d.rectangle([x, 180 - h // 2, x + 14, 180 + h // 2], fill=(120, 130, 255))
        d.text((150, 250), 'AUDIO', fill=(140, 148, 170))
    else:
        label = (ext or '').lstrip('.').upper() or 'FILE'
        d.rectangle([120, 140, 240, 220], outline=(90, 100, 125), width=2)
        d.text((150, 250), label, fill=(140, 148, 170))
    im.save(out, 'WEBP', quality=80, method=2)
    return out


def _ui_font(size, bold=False):
    from PIL import ImageFont
    for p in (('C:/Windows/Fonts/msyhbd.ttc' if bold else 'C:/Windows/Fonts/msyh.ttc'),
              'C:/Windows/Fonts/msyhbd.ttc', 'C:/Windows/Fonts/arialbd.ttf'):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _doc_placeholder(out, ext, title):
    from PIL import Image, ImageDraw
    im = Image.new('RGB', (480, 300), (22, 26, 35))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 480, 7], fill=(139, 125, 255))
    label = (ext or '').lstrip('.').upper() or 'DOC'
    d.rounded_rectangle([30, 36, 116, 84], 8, fill=(44, 50, 68))
    d.text((44, 48), label, font=_ui_font(24, True), fill=(210, 216, 232))
    t = title or ('PDF 文档' if label == 'PDF' else '演示文稿')
    lines, buf = [], ''
    for ch in t:
        if d.textlength(buf + ch, font=_ui_font(26, True)) > 400:
            lines.append(buf); buf = ch
            if len(lines) >= 2: break
        else:
            buf += ch
    if buf and len(lines) < 2:
        lines.append(buf)
    y = 130
    for line in lines:
        d.text((30, y), line, font=_ui_font(26, True), fill=(232, 236, 246))
        y += 38
    d.line([30, 236, 450, 236], fill=(56, 64, 84), width=1)
    d.text((30, 252), '封面预览', font=_ui_font(15), fill=(120, 130, 152))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    im.save(out, 'WEBP', quality=82, method=2)
    return out


_PDF_LOCK = threading.Lock()


def doc_cover(item_id, path, mtime, ext, out):
    """PDF / PPTX 取第一页或第一张图当封面；老 .ppt 只能出占位封面"""
    try:
        from PIL import Image
        if ext == '.pdf':
            import pypdfium2 as pdfium
            # pdfium 不是线程安全的：首页打开时几十张封面同时请求，必须串行，
            # 否则会出现 pdfium.dll 0xc0000409 整个进程闪退。
            with _PDF_LOCK:
                pdf = pdfium.PdfDocument(path)
                try:
                    page = pdf[0]
                    try:
                        w, _h = page.get_size()
                        scale = max(1.0, 480.0 / max(1, w))
                        im = page.render(scale=scale).to_pil().convert('RGB')
                    finally:
                        try:
                            page.close()
                        except Exception:
                            pass
                finally:
                    try:
                        pdf.close()
                    except Exception:
                        pass
            im.thumbnail((480, 640), Image.LANCZOS)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            im.save(out, 'WEBP', quality=82, method=2)
            return out
        if ext == '.pptx':
            import io
            from pptx import Presentation
            prs = Presentation(path)
            slide = prs.slides[0]
            best = None
            for sh in slide.shapes:
                try:
                    if getattr(sh, 'shape_type', None) == 13 or hasattr(sh, 'image'):
                        blob = sh.image.blob
                        im = Image.open(io.BytesIO(blob)).convert('RGB')
                        if best is None or im.width * im.height > best.width * best.height:
                            best = im
                except Exception:
                    continue
            if best is not None:
                best.thumbnail((480, 640), Image.LANCZOS)
                os.makedirs(os.path.dirname(out), exist_ok=True)
                best.save(out, 'WEBP', quality=82, method=2)
                return out
            title = ''
            try:
                title = (slide.shapes.title.text_frame.text or '').strip()
            except Exception:
                pass
            return _doc_placeholder(out, ext, title or os.path.splitext(os.path.basename(path))[0])
    except Exception:
        pass
    return _doc_placeholder(out, ext, os.path.splitext(os.path.basename(path))[0])


def thumb_for(item_id, path, mtime, media='图片', ext=''):
    key = '%d_%d.webp' % (item_id, mtime)
    out = os.path.join(THUMB_DIR, key[:2], key)
    if os.path.exists(out):
        return out
    if media == '文档':
        return doc_cover(item_id, path, mtime, ext, out)
    if media == '视频':
        try:
            import imageio_ffmpeg, subprocess, tempfile
            exe = imageio_ffmpeg.get_ffmpeg_exe()
            tmp = os.path.join(tempfile.gettempdir(), 'libthumb_%d.jpg' % item_id)
            ok = False
            for ts in ('0', '0.3', '1', '2', '4', '8'):
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                try:
                    # CREATE_NO_WINDOW：否则每抽一帧都会弹一个 ffmpeg 黑窗口
                    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                    subprocess.run([exe, '-y', '-loglevel', 'quiet', '-ss', ts, '-i', path,
                                    '-frames:v', '1', '-vf', 'scale=480:-2', '-q:v', '3', tmp],
                                   timeout=20, check=False, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=flags)
                except Exception:
                    continue
                if os.path.exists(tmp) and os.path.getsize(tmp) > 1024:
                    ok = True
                    break
            if ok:
                from PIL import Image
                with Image.open(tmp) as im:
                    im = im.convert('RGB')
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                    im.save(out, 'WEBP', quality=80, method=2)
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                return out
        except Exception:
            pass
        return placeholder_thumb('视频', ext)
    if media != '图片':
        return placeholder_thumb(media, ext)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    try:
        from PIL import Image, ImageOps
        with _thumb_lock:
            im = Image.open(path)
            im = ImageOps.exif_transpose(im)
            if im.mode in ('P', 'LA', 'CMYK'):
                im = im.convert('RGBA')
            im.thumbnail((360, 360), Image.LANCZOS)
            im.save(out, 'WEBP', quality=80, method=2)
        return out
    except Exception:
        return placeholder_thumb('文件', ext)


def image_size(path):
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        return (None, None)


# ---------------- 尺寸 / 透明 / 时长补建 ----------------
# 属性筛选（方图/竖图/透明底/短视频）依赖 items.w/h/alpha/dur。
# 旧索引这些字段是空的，新扫描也不会主动解码，所以放到后台低优先级补建。
META_JOB = {'running': False, 'phase': 'idle', 'done': 0, 'total': 0,
            'images': 0, 'videos': 0, 'msg': ''}
META_START_LOCK = threading.Lock()
ALPHA_EXT = {'.png', '.webp', '.gif', '.tif', '.tiff', '.psd', '.ico',
             '.avif', '.heic', '.heif', '.jxl', '.svg'}


def _alpha_of(path, ext):
    """只对可能带透明通道的格式开一次 Pillow，JPEG/BMP 直接跳过。"""
    if ext == '.svg':
        return 1
    if ext not in ALPHA_EXT:
        return 0
    try:
        from PIL import Image
        with Image.open(path) as im:
            return 1 if (im.mode in ('RGBA', 'LA', 'PA') or 'transparency' in im.info) else 0
    except Exception:
        return 0


def _image_metadata(path, ext):
    """返回 (w,h,alpha)。优先 imagesize 读文件头，失败再退回 Pillow。"""
    w = h = 0
    if imagesize is not None:
        try:
            w, h = imagesize.get(path)
        except Exception:
            w = h = 0
    if (not w or not h or w < 0 or h < 0):
        try:
            from PIL import Image
            with Image.open(path) as im:
                w, h = im.size
        except Exception:
            w = h = 0
    return int(w or 0), int(h or 0), _alpha_of(path, ext)


def meta_pending():
    con = connect()
    try:
        img = con.execute("SELECT COUNT(*) FROM items WHERE media='图片' AND meta_state=0").fetchone()[0]
        vid = con.execute("SELECT COUNT(*) FROM items WHERE media='视频' AND meta_state=0").fetchone()[0]
    finally:
        con.close()
    return img, vid


def start_meta_index():
    """后台补建尺寸/透明/时长；已经跑或没有待补数据就返回 False。"""
    with META_START_LOCK:
        if META_JOB.get('running'):
            return False
        img, vid = meta_pending()
        if img + vid <= 0:
            META_JOB.update(images=0, videos=0, total=0, done=0, phase='ready', msg='无需补建')
            return False
        META_JOB.update(running=True, phase='准备', done=0, total=img + vid,
                        images=img, videos=vid, msg='正在补建尺寸/透明/时长')
    threading.Thread(target=_meta_worker, daemon=True).start()
    return True


def _meta_worker():
    _low_priority()
    done = 0
    try:
        while True:
            con = connect()
            rows = con.execute(
                "SELECT id,path,ext,media FROM items WHERE meta_state=0 "
                "AND media IN ('图片','视频') ORDER BY media LIMIT 60").fetchall()
            if not rows:
                con.close()
                break
            for item_id, path, ext, media in rows:
                if media == '图片':
                    w, h, alpha = _image_metadata(path, (ext or '').lower())
                    if w and h:
                        con.execute('UPDATE items SET w=?,h=?,alpha=?,meta_state=1 WHERE id=?',
                                    (w, h, alpha, item_id))
                    else:
                        con.execute('UPDATE items SET meta_state=2 WHERE id=?', (item_id,))
                else:
                    dur, _vc = _probe_video(path)
                    if dur and dur > 0:
                        con.execute('UPDATE items SET dur=?,meta_state=1 WHERE id=?',
                                    (float(dur), item_id))
                    else:
                        con.execute('UPDATE items SET meta_state=2 WHERE id=?', (item_id,))
            con.commit()
            con.close()
            done += len(rows)
            META_JOB.update(done=done, total=max(META_JOB.get('total', 0), done),
                            msg='已补建 %d 项' % done)
        bump_data()
        META_JOB.update(running=False, phase='ready', done=done, total=done,
                        images=0, videos=0, msg='尺寸/透明/时长补建完成')
        print('媒体属性补建完成：%d 项' % done)
    except Exception as e:
        META_JOB.update(running=False, phase='error', msg=str(e))
        print('媒体属性补建失败：%s' % e)


def _filter(p):
    """把筛选参数拼成 SQL WHERE 和参数列表"""
    where, args = [], []
    if p.get('type') and p['type'] != '全部':
        where.append('media=?'); args.append(p['type'])
    if p.get('cat') and p['cat'] != '全部':
        where.append('cat=?'); args.append(p['cat'])
    if p.get('loc') and p['loc'] != '全部':
        where.append('loc=?'); args.append(p['loc'])
    if p.get('disk'):
        where.append('path LIKE ?'); args.append(p['disk'] + '%')
    if p.get('sub'):
        where.append('sub=?'); args.append(p['sub'])
    if p.get('tag'):
        where.append('EXISTS (SELECT 1 FROM item_tags it JOIN tags t ON t.id=it.tag_id '
                     'WHERE it.item_id=items.id AND t.name=?)'); args.append(p['tag'])
    if p.get('fav'):
        where.append('EXISTS (SELECT 1 FROM fav f WHERE f.item_id=items.id)')
    if p.get('q'):
        where.append('(name LIKE ? OR path LIKE ? OR content LIKE ? OR content LIKE ?)')
        q = p['q']
        q2 = re.sub(r'\s+', '', q)
        args += ['%' + q + '%', '%' + q + '%', '%' + q + '%', '%' + q2 + '%']
    if p.get('ext'):
        where.append('name LIKE ?'); args.append('%' + p['ext'])
    if p.get('orient'):
        if p['orient'] == 'square':
            where.append('w IS NOT NULL AND h IS NOT NULL AND ABS(w-h)<=MAX(2,MIN(w,h)*0.04)')
        elif p['orient'] == 'portrait':
            where.append('w IS NOT NULL AND h IS NOT NULL AND h>w*1.12')
        elif p['orient'] == 'landscape':
            where.append('w IS NOT NULL AND h IS NOT NULL AND w>h*1.12')
    if p.get('minw'):
        try:
            where.append('w>=?'); args.append(int(p['minw']))
        except ValueError:
            pass
    if p.get('minh'):
        try:
            where.append('h>=?'); args.append(int(p['minh']))
        except ValueError:
            pass
    if p.get('alpha'):
        where.append('alpha=1')
    if p.get('durmin'):
        try:
            where.append('dur>=?'); args.append(float(p['durmin']))
        except ValueError:
            pass
    if p.get('durmax'):
        try:
            where.append('dur>0 AND dur<=?'); args.append(float(p['durmax']))
        except ValueError:
            pass
    if p.get('recent'):
        try:
            days = int(p['recent'])
            where.append('mtime >= ?'); args.append(int(time.time()) - 86400 * days)
        except ValueError:
            pass
    return (('WHERE ' + ' AND '.join(where)) if where else ''), args


# ---------------- 视频转码（浏览器不支持的容器） ----------------
PLAY_JOBS = {}
PLAY_LOCK = threading.Lock()
_VID_OK = ('h264', 'vp8', 'vp9', 'av1')
_MP4_CODEC_TAGS = {
    b'avc1': 'avc1', b'avc3': 'avc3', b'hvc1': 'hvc1', b'hev1': 'hev1',
    b'av01': 'av01', b'vp09': 'vp09',
}
_MP4CODEC = {'avc1': 'h264', 'avc3': 'h264', 'hvc1': 'hevc', 'hev1': 'hevc',
             'av01': 'av1', 'vp09': 'vp9'}


def _mp4_codec_hint(path):
    """不启动 ffmpeg，直接扫 MP4 头/尾的 sample entry，快速判断编码。
    这样普通 H.264 MP4 打开时不必再等一次 ffmpeg -i。"""
    try:
        size = os.path.getsize(path)
        data = b''
        with open(path, 'rb') as f:
            data = f.read(min(size, 2 * 1024 * 1024))
            if size > 4 * 1024 * 1024:
                f.seek(max(0, size - 2 * 1024 * 1024))
                data += f.read(2 * 1024 * 1024)
        for tag, name in _MP4_CODEC_TAGS.items():
            if tag in data:
                return name
    except Exception:
        pass
    return ''


def _ffmpeg_exe():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _probe_video(path):
    """用 ffmpeg -i 读时长和视频编码；失败返回 (0, '')"""
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        p = subprocess.run([_ffmpeg_exe(), '-hide_banner', '-i', path],
                           capture_output=True, timeout=20, creationflags=flags,
                           stdin=subprocess.DEVNULL)
        txt = (p.stderr or b'').decode('utf-8', 'replace')
        dur = 0.0
        m = re.search(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)', txt)
        if m:
            dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        vc = ''
        m = re.search(r'Stream #.*?Video: ([A-Za-z0-9_]+)', txt)
        if m:
            vc = m.group(1).lower()
        return dur, vc
    except Exception:
        return 0.0, ''


def play_cache_path(item_id, mtime):
    return os.path.join(PLAY_DIR, '%d_%d.mp4' % (item_id, mtime))


def _clean_play_cache():
    """播放缓存超过 30 天就删（只删缓存，不动原文件）"""
    try:
        now = time.time()
        for f in os.listdir(PLAY_DIR):
            p = os.path.join(PLAY_DIR, f)
            if os.path.isfile(p) and now - os.path.getmtime(p) > 86400 * 30:
                try:
                    os.remove(p)
                except OSError:
                    pass
    except OSError:
        pass


def _run_transcode(item_id, src, out, dur):
    job = PLAY_JOBS.get(item_id)
    tmp = out + '.part.mp4'
    try:
        os.makedirs(PLAY_DIR, exist_ok=True)
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        cmd = [_ffmpeg_exe(), '-y', '-hide_banner', '-loglevel', 'error',
               '-i', src, '-map', '0:v:0', '-map', '0:a:0?',
               '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '26',
               '-vf', 'scale=min(960\\,iw):min(960\\,ih):force_original_aspect_ratio=decrease:force_divisible_by=2',
               '-pix_fmt', 'yuv420p', '-threads', '4',
               '-c:a', 'aac', '-b:a', '96k', '-ac', '2',
               '-movflags', '+faststart', '-progress', 'pipe:1', '-nostats', tmp]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, bufsize=1,
                                encoding='utf-8', errors='replace', creationflags=flags)
        tail = []
        for line in proc.stdout:
            line = line.strip()
            if line.startswith('out_time_ms='):
                try:
                    sec = int(line.split('=', 1)[1]) / 1e6
                    if dur > 0 and job is not None:
                        job['pct'] = max(job.get('pct', 0), max(0, min(99, int(sec / dur * 100))))
                except ValueError:
                    pass
            elif line:
                tail.append(line)
                tail = tail[-5:]
        code = proc.wait()
        if code == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 1024:
            os.replace(tmp, out)
            if job is not None:
                job['pct'] = 100
                job['state'] = 'ready'
                job['msg'] = '已转好'
        else:
            try:
                os.remove(tmp)
            except OSError:
                pass
            if job is not None:
                job['state'] = 'error'
                job['msg'] = '转码失败：' + (' / '.join(tail)[-160:] or '视频可能损坏')
    except Exception as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        if job is not None:
            job['state'] = 'error'
            job['msg'] = '转码失败：' + str(e)[:160]


_MPV_PROC = {'proc': None, 'id': None}


def mpv_exe():
    """内置 mpv：打包版在 _MEIPASS/mpv，源码运行在 _工具/mpv"""
    cands = []
    mei = getattr(sys, '_MEIPASS', '')
    if mei:
        cands.append(os.path.join(mei, 'mpv', 'mpv.exe'))
    cands.append(os.path.join(TOOLS, 'mpv', 'mpv.exe'))
    cands.append(os.path.join(BASE, 'mpv', 'mpv.exe'))
    for p in cands:
        if os.path.exists(p):
            return p
    return ''


def launch_mpv(path, title):
    exe = mpv_exe()
    if not exe:
        return False, '没有找到内置播放器 mpv.exe'
    try:
        old = _MPV_PROC.get('proc')
        if old is not None and old.poll() is None:
            try:
                old.terminate()
                old.wait(timeout=1.0)
            except Exception:
                try:
                    old.kill()
                except Exception:
                    pass
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        proc = subprocess.Popen(
            [exe, '--no-terminal', '--force-window=yes', '--keep-open=no', '--osc=yes',
             '--geometry=80%', '--hwdec=auto-safe', '--title=素材坞 · ' + (title or ''), '--', path],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags)
        _MPV_PROC.update(proc=proc, id=title)
        return True, ''
    except Exception as e:
        return False, str(e)[:160]


def play_state(item_id, path, mtime, ext, allow_transcode=True):
    """返回 {'mode': direct/ready/mpv/transcoding/unsupported/error, 'url','pct','msg','reason'}"""
    codec, reason = '', (ext or '').lstrip('.').upper() + ' 格式'
    if ext in DIRECT_VIDEO:
        if ext in ('.mp4', '.m4v'):
            tag = _mp4_codec_hint(path)
            codec = _MP4CODEC.get(tag, '')
            if not codec or codec in _VID_OK:
                return {'mode': 'direct', 'url': '/file/%d' % item_id, 'codec': codec}
            reason = 'MP4 的 %s 编码' % ('HEVC/H.265' if codec == 'hevc' else codec.upper())
        else:
            return {'mode': 'direct', 'url': '/file/%d' % item_id}
    # 浏览器内核播不了的（MOV/MKV/AVI、HEVC MP4…）优先交给内置 mpv 直接播；
    # 只有显式请求 transcode=1 时才走 540p 预览转码。
    if not allow_transcode and mpv_exe():
        return {'mode': 'mpv', 'reason': reason, 'codec': codec}
    out = play_cache_path(item_id, mtime)
    if os.path.exists(out) and os.path.getsize(out) > 1024:
        return {'mode': 'ready', 'url': '/play/%d' % item_id}
    job = PLAY_JOBS.get(item_id)
    if job is None:
        if not allow_transcode:
            return {'mode': 'unsupported', 'reason': reason, 'codec': codec, 'can_transcode': True}
        dur, _vc = _probe_video(path)
        _clean_play_cache()
        job = {'state': 'running', 'pct': 0, 'msg': '正在生成预览', 't0': time.time()}
        PLAY_JOBS[item_id] = job
        threading.Thread(target=_run_transcode, args=(item_id, path, out, dur),
                         daemon=True).start()
    if job.get('state') == 'error':
        return {'mode': 'error', 'msg': job.get('msg') or '转码失败', 'reason': reason}
    if job.get('state') == 'ready':
        return {'mode': 'ready', 'url': '/play/%d' % item_id}
    return {'mode': 'transcoding', 'pct': job.get('pct', 0), 'msg': job.get('msg', '正在生成预览'),
            'reason': reason}


# ---------------- HTTP ----------------
# ---------------- 待删除箱 ----------------
def parse_ids(ids_s, limit=500):
    ids = []
    for s in (ids_s or '').split(','):
        s = s.strip()
        if s.isdigit() and int(s) > 0 and int(s) not in ids:
            ids.append(int(s))
    return ids[:limit]


def _unique_target(folder, name):
    target = os.path.join(folder, name)
    if not os.path.exists(target):
        return target
    stem, ext = os.path.splitext(name)
    for i in range(2, 200):
        t = os.path.join(folder, '%s (%d)%s' % (stem, i, ext))
        if not os.path.exists(t):
            return t
    return os.path.join(folder, '%s (%s)%s' % (stem, uuid.uuid4().hex[:6], ext))


def _snapshot_links(con, item_id):
    tags = [r[0] for r in con.execute(
        'SELECT t.name FROM item_tags it JOIN tags t ON t.id=it.tag_id WHERE it.item_id=?', (item_id,)).fetchall()]
    fav = 1 if con.execute('SELECT 1 FROM fav WHERE item_id=?', (item_id,)).fetchone() else 0
    return tags, fav


def trash_one(con, item_id, batch, now):
    row = con.execute('SELECT path,name,ext,media,size,mtime FROM items WHERE id=?', (item_id,)).fetchone()
    if not row:
        return {'id': item_id, 'ok': False, 'msg': '没有这条记录'}
    path, name, ext, media, size, mtime = row
    if not os.path.exists(path):
        con.execute('DELETE FROM items WHERE id=?', (item_id,))
        con.execute('DELETE FROM item_tags WHERE item_id=?', (item_id,))
        con.execute('DELETE FROM fav WHERE item_id=?', (item_id,))
        return {'id': item_id, 'ok': False, 'cleaned': True, 'name': name,
                'msg': '文件已不在原位，已清掉索引'}
    tags, fav = _snapshot_links(con, item_id)
    day = time.strftime('%Y%m%d', time.localtime(now))
    destdir = os.path.join(TRASH_DIR, day, batch)
    os.makedirs(destdir, exist_ok=True)
    dest = _unique_target(destdir, '%d_%s' % (item_id, name))
    try:
        shutil.move(path, dest)
    except Exception as e:
        return {'id': item_id, 'ok': False, 'name': name, 'msg': '移到待删除失败：' + str(e)}
    con.execute('INSERT INTO trash(item_id,orig_path,trash_path,name,ext,media,size,mtime,deleted_at,expire_at,batch,tags,fav)'
                ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (item_id, path, dest, name, ext or '', media or '', size or 0, mtime or 0,
                 int(now), int(now + TRASH_DAYS * 86400), batch,
                 json.dumps(tags, ensure_ascii=False), fav))
    con.execute('DELETE FROM item_tags WHERE item_id=?', (item_id,))
    con.execute('DELETE FROM fav WHERE item_id=?', (item_id,))
    con.execute('DELETE FROM items WHERE id=?', (item_id,))
    return {'id': item_id, 'ok': True, 'name': name, 'size': size or 0}


def api_trash_add(self, ids_s):
    ids = parse_ids(ids_s, 5000)
    if not ids:
        self._json({'ok': False, 'msg': '没有选中任何文件'}, 400)
        return
    batch = time.strftime('%Y%m%d_%H%M%S_', time.localtime()) + uuid.uuid4().hex[:6]
    con = connect()
    moved, failed = [], []
    now = time.time()
    for item_id in ids:
        r = trash_one(con, item_id, batch, now)
        (moved if r.get('ok') else failed).append(r)
    con.commit()
    con.close()
    cleaned = sum(1 for r in failed if r.get('cleaned'))
    if moved or cleaned:
        bump_data()
    self._json({'ok': bool(moved or cleaned), 'moved': len(moved), 'cleaned': cleaned,
                'failed': failed, 'batch': batch,
                'names': [m.get('name') for m in moved],
                'bytes': sum((m.get('size') or 0) for m in moved)})


def _clean_expired_trash(con):
    now = time.time()
    rows = con.execute('SELECT id,trash_path FROM trash WHERE expire_at>0 AND expire_at<?', (now,)).fetchall()
    for tid, p in rows:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except OSError:
            continue
        con.execute('DELETE FROM trash WHERE id=?', (tid,))
    if rows:
        con.commit()


def _trash_count():
    try:
        con = connect()
        n = con.execute('SELECT COUNT(*) FROM trash').fetchone()[0]
        con.close()
        return n
    except Exception:
        return 0


def api_trash_list(self):
    con = connect()
    _clean_expired_trash(con)
    rows = con.execute('SELECT id,item_id,orig_path,name,ext,media,size,deleted_at,expire_at,batch,tags,fav'
                       ' FROM trash ORDER BY deleted_at DESC,id DESC').fetchall()
    con.close()
    items = [{'id': r[0], 'item_id': r[1], 'path': r[2], 'name': r[3], 'ext': r[4], 'media': r[5],
              'size': r[6], 'deleted_at': r[7], 'expire_at': r[8], 'batch': r[9],
              'tags': json.loads(r[10] or '[]'), 'fav': bool(r[11])} for r in rows]
    self._json({'items': items, 'count': len(items), 'bytes': sum(x['size'] or 0 for x in items),
                'days': TRASH_DAYS})


def trash_restore(con, tid):
    row = con.execute('SELECT orig_path,trash_path,name,ext,size,mtime,tags,fav FROM trash WHERE id=?', (tid,)).fetchone()
    if not row:
        return {'id': tid, 'ok': False, 'msg': '记录不存在'}
    orig, src, name, ext, size, mtime, tags_s, fav = row
    if not src or not os.path.exists(src):
        con.execute('DELETE FROM trash WHERE id=?', (tid,))
        return {'id': tid, 'ok': False, 'name': name, 'msg': '待删除箱里的文件已丢失'}
    folder = os.path.dirname(orig)
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as e:
        return {'id': tid, 'ok': False, 'name': name, 'msg': '原目录不可写：' + str(e)}
    target = orig if not os.path.exists(orig) else _unique_target(folder, os.path.splitext(name)[0] + ' (还原)' + (ext or ''))
    try:
        shutil.move(src, target)
    except Exception as e:
        return {'id': tid, 'ok': False, 'name': name, 'msg': '还原失败：' + str(e)}
    st = os.stat(target)
    media = media_of(os.path.splitext(target)[1])
    con.execute('INSERT INTO items(path,name,ext,media,size,mtime,loc,cat,sub,seen_run)'
                ' VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET name=excluded.name,'
                ' ext=excluded.ext,media=excluded.media,size=excluded.size,mtime=excluded.mtime,'
                ' loc=excluded.loc,cat=excluded.cat,sub=excluded.sub,seen_run=excluded.seen_run',
                (target, os.path.basename(target), os.path.splitext(target)[1].lower(), media or '',
                 st.st_size, int(st.st_mtime), location_of(target), guess_cat(target, media or ''),
                 sub_of(target, media or ''), int(time.time() * 1000)))
    item_id = con.execute('SELECT id FROM items WHERE path=?', (target,)).fetchone()[0]
    for name in json.loads(tags_s or '[]'):
        con.execute('INSERT OR IGNORE INTO tags(name) VALUES(?)', (name,))
        tag_id = con.execute('SELECT id FROM tags WHERE name=?', (name,)).fetchone()[0]
        con.execute('INSERT OR IGNORE INTO item_tags(item_id,tag_id) VALUES(?,?)', (item_id, tag_id))
    if fav:
        con.execute('INSERT OR IGNORE INTO fav(item_id,created_at) VALUES(?,?)', (item_id, int(time.time())))
    con.execute('DELETE FROM trash WHERE id=?', (tid,))
    return {'id': tid, 'ok': True, 'name': os.path.basename(target), 'path': target}


def api_trash_restore(self, ids_s):
    ids = parse_ids(ids_s, 500)
    if not ids:
        self._json({'ok': False, 'msg': '没有指定要还原的记录'}, 400)
        return
    con = connect()
    ok, failed = [], []
    for tid in ids:
        r = trash_restore(con, tid)
        (ok if r.get('ok') else failed).append(r)
    con.commit(); con.close()
    if ok:
        bump_data()
    self._json({'ok': True, 'restored': len(ok), 'failed': failed})


def api_trash_undo(self):
    con = connect()
    batch = con.execute('SELECT batch FROM trash ORDER BY deleted_at DESC,id DESC LIMIT 1').fetchone()
    if not batch or not batch[0]:
        con.close()
        self._json({'ok': False, 'msg': '没有可撤销的删除'}, 404)
        return
    ids = [r[0] for r in con.execute('SELECT id FROM trash WHERE batch=?', (batch[0],)).fetchall()]
    con.close()
    api_trash_restore(self, ','.join(str(i) for i in ids))


def api_trash_clear(self):
    con = connect()
    rows = con.execute('SELECT trash_path FROM trash').fetchall()
    removed = 0
    for (p,) in rows:
        try:
            if p and os.path.exists(p):
                os.remove(p)
                removed += 1
        except OSError:
            pass
    con.execute('DELETE FROM trash')
    con.commit(); con.close()
    bump_data()
    self._json({'ok': True, 'removed': removed})


# ---------------- 标签、收藏 ----------------
def _tag_list(con, item_id):
    return [r[0] for r in con.execute(
        'SELECT t.name FROM item_tags it JOIN tags t ON t.id=it.tag_id WHERE it.item_id=? ORDER BY t.name',
        (item_id,)).fetchall()]


def api_tag_add(self, ids_s, name):
    ids = parse_ids(ids_s, 500)
    name = (name or '').strip()
    if not name or len(name) > 24:
        self._json({'ok': False, 'msg': '标签需为 1–24 个字'}, 400)
        return
    con = connect()
    con.execute('INSERT OR IGNORE INTO tags(name) VALUES(?)', (name,))
    tag_id = con.execute('SELECT id FROM tags WHERE name=?', (name,)).fetchone()[0]
    n = 0
    for item_id in ids:
        if con.execute('SELECT 1 FROM items WHERE id=?', (item_id,)).fetchone():
            con.execute('INSERT OR IGNORE INTO item_tags(item_id,tag_id) VALUES(?,?)', (item_id, tag_id))
            n += 1
    con.commit(); con.close()
    if n:
        bump_data()
    self._json({'ok': True, 'tag': name, 'count': n})


def api_tag_remove(self, ids_s, name):
    ids = parse_ids(ids_s, 500)
    con = connect()
    tag_id = con.execute('SELECT id FROM tags WHERE name=?', ((name or '').strip(),)).fetchone()
    if not tag_id:
        con.close()
        self._json({'ok': False, 'msg': '没有这个标签'}, 404)
        return
    n = 0
    for item_id in ids:
        cur = con.execute('DELETE FROM item_tags WHERE item_id=? AND tag_id=?', (item_id, tag_id[0]))
        n += cur.rowcount
    con.commit(); con.close()
    bump_data()
    self._json({'ok': True, 'tag': name, 'removed': n})


def api_tag_delete(self, name):
    name = (name or '').strip()
    con = connect()
    row = con.execute('SELECT id FROM tags WHERE name=?', (name,)).fetchone()
    if row:
        con.execute('DELETE FROM item_tags WHERE tag_id=?', (row[0],))
        con.execute('DELETE FROM tags WHERE id=?', (row[0],))
        con.commit()
        bump_data()
    con.close()
    self._json({'ok': True})


def api_tags(self):
    con = connect()
    rows = con.execute('SELECT t.name,COUNT(it.item_id) FROM tags t LEFT JOIN item_tags it ON it.tag_id=t.id'
                       ' GROUP BY t.id ORDER BY COUNT(it.item_id) DESC,t.name').fetchall()
    con.close()
    self._json({'tags': [{'name': r[0], 'n': r[1]} for r in rows]})


def api_item_tags(self, item_id):
    con = connect()
    row = con.execute('SELECT id,name,path FROM items WHERE id=?', (item_id,)).fetchone()
    if not row:
        con.close()
        self._json({'error': 'not found'}, 404)
        return
    con.close()
    con = connect()
    tags = _tag_list(con, item_id)
    fav = bool(con.execute('SELECT 1 FROM fav WHERE item_id=?', (item_id,)).fetchone())
    con.close()
    self._json({'id': item_id, 'tags': tags, 'fav': fav})


def api_fav(self, ids_s, on):
    ids = parse_ids(ids_s, 500)
    con = connect()
    n = 0
    for item_id in ids:
        if not con.execute('SELECT 1 FROM items WHERE id=?', (item_id,)).fetchone():
            continue
        if on:
            con.execute('INSERT OR IGNORE INTO fav(item_id,created_at) VALUES(?,?)', (item_id, int(time.time())))
        else:
            con.execute('DELETE FROM fav WHERE item_id=?', (item_id,))
        n += 1
    con.commit(); con.close()
    if n:
        bump_data()
    self._json({'ok': True, 'count': n, 'on': bool(on)})


# ---------------- 哈希、重复清理 ----------------
DEDUPE_JOB = {'running': False, 'phase': 'idle', 'done': 0, 'total': 0, 'msg': '', 't0': 0, 'found': 0}


def _dedupe_set(**kw):
    DEDUPE_JOB.update(kw)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def file_fingerprint(path):
    """小文件算完整 SHA-256；大文件用 大小+首/中/尾各 1MB 的 BLAKE2b，
    避免为了查重把几百 MB 的视频/画册从头读到尾，实际重复判定足够可靠。"""
    size = os.path.getsize(path)
    if size <= 8 * 1024 * 1024:
        return sha256_file(path)
    h = hashlib.blake2b(digest_size=16)
    h.update(str(size).encode('ascii'))
    chunk = 1024 * 1024
    with open(path, 'rb') as f:
        for pos in (0, max(0, size // 2 - chunk // 2), max(0, size - chunk)):
            f.seek(pos)
            h.update(f.read(chunk))
    return 'q:' + h.hexdigest()


def _image_for_hash(path, item_id=None, mtime=None):
    # 优先用已经生成过的缩略图，比每次重开 4K 原图快很多
    if item_id and mtime:
        key = '%d_%d.webp' % (int(item_id), int(mtime))
        t = os.path.join(THUMB_DIR, key[:2], key)
        try:
            if os.path.exists(t):
                from PIL import Image
                return Image.open(t), True
        except Exception:
            pass
    key = None
    try:
        st = os.stat(path)
        key = os.path.join(THUMB_DIR, '_hash', '%s_%d.webp' % (hashlib.md5(path.encode('utf-8')).hexdigest(), int(st.st_mtime)))
        if os.path.exists(key):
            from PIL import Image
            return Image.open(key), True
    except Exception:
        pass
    from PIL import Image, ImageOps
    im = Image.open(path)
    # 先看文件头尺寸，再 EXIF 旋转；否则 exif_transpose 会直接解码上亿像素大图
    if im.width * im.height > 16_000_000:
        im.close()
        raise ValueError('image too large')
    im = ImageOps.exif_transpose(im)
    if im.mode in ('P', 'LA', 'CMYK'):
        im = im.convert('RGBA')
    return im, False


def dhash_file(path, item_id=None, mtime=None):
    """64 位差值哈希，返回 (哈希, 是否透明, 宽, 高)"""
    im, cached = _image_for_hash(path, item_id, mtime)
    try:
        w, h = im.size
        if not cached and w * h > 16_000_000:
            # 上亿像素的超大图解码会占满几分钟 CPU，查相似直接跳过
            raise ValueError('image too large')
        if cached:
            # 缩略图尺寸不是原图尺寸，只读原图文件头补回宽高
            try:
                from PIL import Image
                ow, oh = Image.open(path).size
                if ow and oh:
                    w, h = ow, oh
            except Exception:
                pass
        alpha = 0
        if im.mode in ('RGBA', 'LA') or 'transparency' in getattr(im, 'info', {}):
            a = im.convert('RGBA').getchannel('A')
            if a.getextrema()[0] < 250:
                alpha = 1
        g = im.convert('L').resize((9, 8), 1)
        px = list(g.getdata())
        bits = 0
        for y in range(8):
            for x in range(8):
                bits = (bits << 1) | (1 if px[y * 9 + x] > px[y * 9 + x + 1] else 0)
        return '%016x' % bits, alpha, w, h
    finally:
        if not cached:
            try:
                im.close()
            except Exception:
                pass


def _hamming(a, b):
    return (int(a, 16) ^ int(b, 16)).bit_count()


def start_dedupe():
    if DEDUPE_JOB.get('running'):
        return False
    DEDUPE_JOB.update({'running': True, 'phase': '准备', 'done': 0, 'total': 0, 'msg': '正在找候选…',
                       't0': time.time(), 'found': 0})
    threading.Thread(target=_dedupe_worker, daemon=True).start()
    return True


def _cloud_path(path):
    p = (path or '').lower().replace('/', '\\')
    return any(k in p for k in ('\\wps cloud', '\\onedrive', '\\dropbox', '\\google drive',
                                '\\iclouddrive', '\\cachedata\\'))


def _sha_row(row):
    item_id, path, mtime, size = row
    try:
        if _cloud_path(path):
            return item_id, '!cloud', mtime
        # 先用索引里的大小判断，避免为几百 MB 的老文件去碰可能休眠的外接盘
        if (size or 0) > 64 * 1024 * 1024:
            return item_id, '!big', mtime
        if not os.path.exists(path):
            return item_id, '', mtime
        st = os.stat(path)
        # 云盘占位文件读取会触发下载/长时间等待，直接跳过
        if getattr(st, 'st_file_attributes', 0) & 0x400000:
            return item_id, '!cloud', mtime
        return item_id, file_fingerprint(path), mtime
    except Exception:
        return item_id, '', mtime


def _hash_row(row):
    item_id, path, mtime, size = row
    try:
        if _cloud_path(path):
            return item_id, '!cloud', 0, 0, 0, mtime
        if not os.path.exists(path):
            return item_id, None, 0, 0, 0, mtime
        return (item_id,) + dhash_file(path, item_id, mtime) + (mtime,)
    except Exception:
        return item_id, None, 0, 0, 0, mtime


def _dedupe_worker():
    _low_priority()
    con = None
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        con = connect()
        size_rows = con.execute(
            'SELECT id,path,mtime,size FROM items WHERE size>0 AND size IN (SELECT size FROM items GROUP BY size HAVING COUNT(*)>1)'
            ' AND (sha="" OR hash_mtime<mtime)').fetchall()
        img_rows = con.execute(
            'SELECT id,path,mtime,size FROM items WHERE media="图片"'
            ' AND ext IN (".jpg",".jpeg",".png",".webp",".gif",".bmp",".tif",".tiff",".avif")'
            ' AND (phash="" OR hash_mtime<mtime OR w IS NULL)').fetchall()
        img_rows = [r for r in img_rows if (r[3] or 0) <= 50 * 1024 * 1024 and not _cloud_path(r[1])]
        def _has_thumb(r):
            try:
                k = '%d_%d.webp' % (int(r[0]), int(r[2]))
                return os.path.exists(os.path.join(THUMB_DIR, k[:2], k))
            except Exception:
                return False
        # 大图没有缩略图时解码很慢，先跳过；浏览过一次生成缩略图后再分析会快很多
        img_rows = [r for r in img_rows if (r[3] or 0) <= 1.5 * 1024 * 1024 or _has_thumb(r)]
        vid_ids = [r[0] for r in con.execute('SELECT id FROM items WHERE media="视频" AND dur<=0').fetchall()]
        total = len(size_rows) + len(img_rows) + len(vid_ids)
        _dedupe_set(total=total, msg='共 %d 个文件需要分析' % total)
        done = 0
        # 并行读盘 / 解码；SQLite 写入仍只在主线程，避免锁竞争
        # PIL 的 resize/convert 会较长时间持有 GIL，线程太多反而饿死主线程的进度/写库
        workers = min(2, os.cpu_count() or 2)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_sha_row, r) for r in size_rows]
            for fut in as_completed(futs):
                item_id, sha, mt = fut.result()
                con.execute('UPDATE items SET sha=?,hash_mtime=? WHERE id=?', (sha, mt, item_id))
                done += 1
                if done % 30 == 0:
                    con.commit()
                    _dedupe_set(done=done, phase='查重', msg='正在计算文件指纹 %d/%d' % (done, total))
        con.commit()
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_hash_row, r) for r in img_rows]
            for fut in as_completed(futs):
                item_id, ph, alpha, w, h, mt = fut.result()
                if ph is None or ph == '!cloud':
                    con.execute('UPDATE items SET hash_mtime=? WHERE id=?', (mt, item_id))
                else:
                    con.execute('UPDATE items SET phash=?,alpha=?,w=COALESCE(w,?),h=COALESCE(h,?),hash_mtime=? WHERE id=?',
                                (ph, alpha, w, h, mt, item_id))
                done += 1
                if done % 25 == 0:
                    con.commit()
                    _dedupe_set(done=done, phase='查相似', msg='正在看图片 %d/%d' % (done, total))
        con.commit()
        for item_id in vid_ids:
            row = con.execute('SELECT path FROM items WHERE id=?', (item_id,)).fetchone()
            if row:
                try:
                    dur, _ = _probe_video(row[0])
                    con.execute('UPDATE items SET dur=? WHERE id=?', (dur or 0, item_id))
                except Exception:
                    pass
            done += 1
            if done % 50 == 0:
                con.commit()
                _dedupe_set(done=done, phase='视频时长', msg='正在读视频信息 %d/%d' % (done, total))
        con.commit()
        bump_data()
        _dedupe_set(running=False, phase='ready', done=total, msg='分析完成')
    except Exception as e:
        _dedupe_set(running=False, phase='error', msg=str(e))
    finally:
        if con:
            try:
                con.close()
            except Exception:
                pass


def dup_exact_plan():
    """返回所有完全重复组里的非保留项，用于“一键清理完全重复”"""
    con = connect()
    rows = con.execute('SELECT id,size,mtime,w,h,sha FROM items WHERE sha<>"" AND sha NOT LIKE "!%"').fetchall()
    con.close()
    groups = {}
    for r in rows:
        groups.setdefault(r[5], []).append(r)
    ids, wasted = [], 0
    for arr in groups.values():
        if len(arr) < 2:
            continue
        best = max(arr, key=lambda x: ((x[3] or 0) * (x[4] or 0), x[1] or 0, x[2] or 0))
        for x in arr:
            if x[0] != best[0]:
                ids.append(x[0])
        wasted += max(0, sum((x[1] or 0) for x in arr) - (best[1] or 0))
    return {'ok': True, 'ids': ids, 'groups': sum(1 for a in groups.values() if len(a) > 1), 'bytes': wasted}


def dup_groups():
    key = _DATA_VER
    cached = _DUP_CACHE.get('groups')
    if cached and cached[0] == key:
        return cached[1]
    con = connect()
    rows = con.execute('SELECT id,name,path,ext,media,size,mtime,w,h,sha,phash,cat,sub,loc FROM items').fetchall()
    cols = ['id', 'name', 'path', 'ext', 'media', 'size', 'mtime', 'w', 'h', 'sha', 'phash', 'cat', 'sub', 'loc']
    items = [dict(zip(cols, r)) for r in rows]

    def best_of(ids):
        return max(ids, key=lambda x: ((x.get('w') or 0) * (x.get('h') or 0), x.get('size') or 0, x.get('mtime') or 0))

    def pack(ids, kind, dist=0):
        chosen = best_of(ids)
        total = sum((x.get('size') or 0) for x in ids)
        return {'kind': kind, 'items': ids, 'best': chosen['id'], 'wasted': max(0, total - (chosen.get('size') or 0)),
                'distance': dist}
    exact = []
    by_sha = {}
    for x in items:
        if x['sha'] and not x['sha'].startswith('!'):
            by_sha.setdefault(x['sha'], []).append(x)
    for sha, arr in by_sha.items():
        if len(arr) > 1:
            exact.append(pack(arr, 'exact'))
    exact.sort(key=lambda g: g['wasted'], reverse=True)

    # 相似图：先按 sha 合并完全相同的文件，避免同一个文件在“完全重复”和“相似”里重复计算
    raw_imgs = [x for x in items if x['media'] == '图片' and x['phash']
                and 8 <= int(x['phash'], 16).bit_count() <= 56]
    reps = {}
    imgs = []
    for x in raw_imgs:
        if x['sha'] and not x['sha'].startswith('!'):
            cur = reps.get(x['sha'])
            if cur is None or ((x.get('size') or 0), (x.get('mtime') or 0)) > ((cur.get('size') or 0), (cur.get('mtime') or 0)):
                reps[x['sha']] = x
        else:
            imgs.append(x)
    imgs += list(reps.values())
    parent = list(range(len(imgs)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    band_buckets = [{} for _ in range(8)]
    for i, x in enumerate(imgs):
        v = int(x['phash'], 16)
        for b in range(8):
            band_buckets[b].setdefault((v >> (b * 8)) & 0xff, []).append(i)
    for bucket in band_buckets:
        for arr in bucket.values():
            if len(arr) < 2 or len(arr) > 350:
                continue
            for ai in range(len(arr)):
                for bi in range(ai + 1, len(arr)):
                    a, b = arr[ai], arr[bi]
                    if find(a) == find(b):
                        continue
                    if _hamming(imgs[a]['phash'], imgs[b]['phash']) <= 2:
                        union(a, b)
    clusters = {}
    for i in range(len(imgs)):
        clusters.setdefault(find(i), []).append(imgs[i])
    similar = []
    for arr in clusters.values():
        if len(arr) < 2 or len(arr) > 20:
            continue
        if all(x['sha'] for x in arr) and len(set(x['sha'] for x in arr)) == 1:
            continue
        similar.append(pack(arr, 'similar', min(_hamming(a['phash'], b['phash']) for a in arr for b in arr if a is not b)))
    similar.sort(key=lambda g: (len(g['items']), g['wasted']), reverse=True)
    result = {'exact': exact[:80], 'similar': similar[:80],
              'exact_count': len(exact), 'similar_count': len(similar),
              'wasted': sum(g['wasted'] for g in exact) + sum(g['wasted'] for g in similar)}
    con.close()
    _DUP_CACHE['groups'] = (key, result)
    return result


# ---------------- 内容索引（PDF/PPT 文本 + 图片 OCR） ----------------
CONTENT_JOB = {'running': False, 'phase': 'idle', 'done': 0, 'total': 0, 'msg': '', 't0': 0, 'ocr': False}
_OCR_ENGINE = None
_OCR_TRIED = False


def _content_set(**kw):
    CONTENT_JOB.update(kw)


def ocr_available():
    global _OCR_ENGINE, _OCR_TRIED
    if _OCR_TRIED:
        return _OCR_ENGINE is not None
    _OCR_TRIED = True
    try:
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.globalization import Language
        eng = OcrEngine.try_create_from_language(Language('zh-Hans'))
        if eng is None:
            eng = OcrEngine.try_create_from_user_profile_languages()
        _OCR_ENGINE = eng
    except Exception:
        _OCR_ENGINE = None
    return _OCR_ENGINE is not None


def _clean_text(s):
    s = re.sub(r'\s+', ' ', s or '')
    s = re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '', s)
    return s.strip()


def ocr_file(path):
    if not ocr_available():
        return ''
    import asyncio
    from winrt.windows.storage import StorageFile, FileAccessMode
    from winrt.windows.graphics.imaging import BitmapDecoder

    async def run():
        f = await StorageFile.get_file_from_path_async(path)
        s = await f.open_async(FileAccessMode.READ)
        dec = await BitmapDecoder.create_async(s)
        bmp = await dec.get_software_bitmap_async()
        res = await _OCR_ENGINE.recognize_async(bmp)
        return res.text or ''

    try:
        # 截图 OCR 常在中英文/数字之间插空格；中文搜索不分词，直接去掉全部空白
        return re.sub(r'\s+', '', asyncio.run(run()))
    except Exception:
        return ''


def pdf_text(path):
    try:
        import pypdfium2 as pdfium
        out = []
        with _PDF_LOCK:
            pdf = pdfium.PdfDocument(path)
            try:
                for i in range(min(len(pdf), 30)):
                    page = textpage = None
                    try:
                        page = pdf[i]
                        textpage = page.get_textpage()
                        out.append(textpage.get_text_range())
                    except Exception:
                        pass
                    finally:
                        try:
                            if textpage is not None:
                                textpage.close()
                        except Exception:
                            pass
                        try:
                            if page is not None:
                                page.close()
                        except Exception:
                            pass
            finally:
                try:
                    pdf.close()
                except Exception:
                    pass
        return _clean_text(' '.join(out))
    except Exception:
        return ''


def pptx_text(path):
    try:
        from pptx import Presentation
        prs = Presentation(path)
        out = []
        for slide in prs.slides[:30]:
            for sh in slide.shapes:
                try:
                    if hasattr(sh, 'text') and sh.text:
                        out.append(sh.text)
                    if getattr(sh, 'has_table', False):
                        for row in sh.table.rows:
                            for cell in row.cells:
                                out.append(cell.text)
                except Exception:
                    pass
        return _clean_text(' '.join(out))
    except Exception:
        return ''


def start_content(scope='smart'):
    if CONTENT_JOB.get('running'):
        return False
    CONTENT_JOB.update({'running': True, 'phase': '准备', 'done': 0, 'total': 0, 'msg': '正在挑文件…',
                        't0': time.time(), 'ocr': ocr_available(), 'stop': False})
    threading.Thread(target=_content_worker, args=(scope,), daemon=True).start()
    return True


def _content_worker(scope):
    _low_priority()
    con = None
    try:
        con = connect()
        docs = con.execute('SELECT id FROM items WHERE media="文档" AND (content_state=0 OR content_mtime<mtime)'
                           ' ORDER BY mtime DESC').fetchall()
        if scope == 'docs':
            imgs = []
        elif scope == 'all':
            imgs = con.execute('SELECT id FROM items WHERE media="图片" AND content_state<>2 AND (content_state=0 OR content_mtime<mtime)'
                               ' AND size<=25*1024*1024 ORDER BY mtime DESC').fetchall()
        else:
            imgs = con.execute('SELECT id FROM items WHERE media="图片" AND content_state<>2 AND (content_state=0 OR content_mtime<mtime)'
                               ' AND size<=25*1024*1024 AND cat IN ("截图","表情包","证照文档","品牌宣传","产品物料")'
                               ' ORDER BY mtime DESC LIMIT 3000').fetchall()
        ids = [r[0] for r in docs] + [r[0] for r in imgs]
        total = len(ids)
        _content_set(total=total, msg='共 %d 个文件待索引' % total)
        for i, item_id in enumerate(ids, 1):
            if CONTENT_JOB.get('stop'):
                _content_set(running=False, phase='stopped', msg='已停止，进度已保存')
                con.commit()
                return
            row = con.execute('SELECT path,media,ext,mtime FROM items WHERE id=?', (item_id,)).fetchone()
            text = ''
            state = 2
            if row and os.path.exists(row[0]):
                path, media, ext, mtime = row
                try:
                    if ext == '.pdf':
                        text = pdf_text(path)
                    elif ext in ('.ppt', '.pptx'):
                        text = pptx_text(path) if ext == '.pptx' else ''
                    elif media == '图片':
                        if ocr_available():
                            text = ocr_file(path)
                        else:
                            state = 3
                except Exception:
                    state = 3
            else:
                state = 3
            con.execute('UPDATE items SET content=?,content_state=?,content_mtime=? WHERE id=?',
                        (text[:20000], state, (row[3] if row else 0), item_id))
            if i % 10 == 0:
                con.commit()
                _content_set(done=i, phase='索引中', msg='正在读 %d/%d：%s' % (i, total, (row[1] if row else '')))
        con.commit()
        bump_data()
        _content_set(running=False, phase='ready', done=total, msg='内容索引完成')
    except Exception as e:
        _content_set(running=False, phase='error', msg=str(e))
    finally:
        if con:
            try:
                con.close()
            except Exception:
                pass


# ---------------- 批量整理 ----------------
def api_batch_rename(self, ids_s, pattern, start=1):
    ids = parse_ids(ids_s, 300)
    pattern = (pattern or '').strip()
    if not ids or not pattern:
        self._json({'ok': False, 'msg': '请选中文件并填写命名模板'}, 400)
        return
    try:
        start = max(1, int(start))
    except ValueError:
        start = 1
    con = connect()
    done, failed = [], []
    for idx, item_id in enumerate(ids, start):
        row = con.execute('SELECT path,name,ext FROM items WHERE id=?', (item_id,)).fetchone()
        if not row or not os.path.exists(row[0]):
            failed.append({'id': item_id, 'msg': '文件已不在原位'})
            continue
        path, old, ext = row
        base = os.path.splitext(old)[0]
        new = pattern.replace('{name}', base).replace('{n}', str(idx).zfill(max(2, len(str(start + len(ids) - 1)))))
        new = new.replace('{date}', time.strftime('%Y%m%d', time.localtime()))
        if '{ext}' not in pattern and not os.path.splitext(new)[1]:
            new += ext or ''
        new = re.sub(r'[\\/:*?"<>|]', '_', new).strip() or ('素材' + ext)
        folder = os.path.dirname(path)
        target = _unique_target(folder, new)
        try:
            os.rename(path, target)
        except OSError as e:
            failed.append({'id': item_id, 'name': old, 'msg': '重命名失败：' + str(e)})
            continue
        st = os.stat(target)
        n_ext = os.path.splitext(target)[1].lower()
        old_media = media_of(ext)
        new_media = media_of(n_ext)
        if new_media != old_media:
            con.execute('UPDATE items SET path=?,name=?,ext=?,media=?,size=?,mtime=?,cat=?,sub=?,'
                        'w=NULL,h=NULL,alpha=0,dur=0,phash=\'\',hash_mtime=0,meta_state=0 WHERE id=?',
                        (target, os.path.basename(target), n_ext, new_media, st.st_size, int(st.st_mtime),
                         guess_cat(target, new_media), sub_of(target, new_media), item_id))
        else:
            con.execute('UPDATE items SET path=?,name=?,ext=?,size=?,mtime=?,cat=?,sub=? WHERE id=?',
                        (target, os.path.basename(target), n_ext, st.st_size, int(st.st_mtime),
                         guess_cat(target, new_media), sub_of(target, new_media), item_id))
        done.append({'id': item_id, 'name': os.path.basename(target)})
    con.commit(); con.close()
    if done:
        bump_data()
        start_meta_index()
    self._json({'ok': True, 'renamed': len(done), 'failed': failed, 'names': [d['name'] for d in done]})


def api_batch_move(self, ids_s, dest, mkdir=0):
    ids = parse_ids(ids_s, 300)
    dest = (dest or '').strip().strip('"')
    if not ids or not dest:
        self._json({'ok': False, 'msg': '请选中文件并填写目标文件夹'}, 400)
        return
    if not os.path.isabs(dest):
        self._json({'ok': False, 'msg': '目标要填完整路径，例如 D:\\素材\\气球'}, 400)
        return
    if not os.path.isdir(dest):
        if mkdir:
            try:
                os.makedirs(dest, exist_ok=True)
            except OSError as e:
                self._json({'ok': False, 'msg': '无法创建目标目录：' + str(e)}, 400)
                return
        else:
            self._json({'ok': False, 'msg': '目标文件夹不存在'}, 400)
            return
    con = connect()
    done, failed = [], []
    for item_id in ids:
        row = con.execute('SELECT path,name FROM items WHERE id=?', (item_id,)).fetchone()
        if not row or not os.path.exists(row[0]):
            failed.append({'id': item_id, 'msg': '文件已不在原位'})
            continue
        path, name = row
        if os.path.normcase(os.path.dirname(path)) == os.path.normcase(dest):
            failed.append({'id': item_id, 'name': name, 'msg': '已经在这个目录里'})
            continue
        target = _unique_target(dest, name)
        try:
            shutil.move(path, target)
        except Exception as e:
            failed.append({'id': item_id, 'name': name, 'msg': '移动失败：' + str(e)})
            continue
        st = os.stat(target)
        media = media_of(os.path.splitext(target)[1])
        con.execute('UPDATE items SET path=?,name=?,size=?,mtime=?,loc=?,cat=?,sub=? WHERE id=?',
                    (target, os.path.basename(target), st.st_size, int(st.st_mtime), location_of(target),
                     guess_cat(target, media), sub_of(target, media), item_id))
        done.append({'id': item_id, 'name': os.path.basename(target), 'path': target})
    con.commit(); con.close()
    if done:
        bump_data()
    self._json({'ok': True, 'moved': len(done), 'failed': failed})


def api_batch_convert(self, ids_s, fmt='webp', quality=85, dest=''):
    ids = parse_ids(ids_s, 150)
    if not ids:
        self._json({'ok': False, 'msg': '没有选中图片'}, 400)
        return
    fmt = 'webp' if (fmt or '').lower() != 'jpg' else 'jpeg'
    try:
        quality = min(95, max(40, int(quality)))
    except ValueError:
        quality = 85
    out_root = (dest or '').strip().strip('"')
    if out_root and not os.path.isabs(out_root):
        self._json({'ok': False, 'msg': '输出目录要填完整路径'}, 400)
        return
    con = connect()
    done, failed, before, after = [], [], 0, 0
    from PIL import Image, ImageOps
    for item_id in ids:
        row = con.execute('SELECT path,name,media,size FROM items WHERE id=?', (item_id,)).fetchone()
        if not row or row[2] != '图片' or not os.path.exists(row[0]):
            failed.append({'id': item_id, 'msg': '不是可转换的图片或文件已丢失'})
            continue
        path, name, media, size = row
        try:
            folder = out_root or os.path.dirname(path)
            os.makedirs(folder, exist_ok=True)
            base = os.path.splitext(name)[0]
            target = _unique_target(folder, base + ('.jpg' if fmt == 'jpeg' else '.webp'))
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im)
                if fmt == 'webp':
                    if im.mode not in ('RGB', 'RGBA'):
                        im = im.convert('RGBA')
                    im.save(target, 'WEBP', quality=quality, method=4)
                else:
                    im = im.convert('RGB')
                    im.save(target, 'JPEG', quality=quality, optimize=True)
            old_size = os.path.getsize(path)
            new_size = os.path.getsize(target)
            st = os.stat(target)
            con.execute('INSERT INTO items(path,name,ext,media,size,mtime,loc,cat,sub,seen_run)'
                        ' VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET size=excluded.size,'
                        ' mtime=excluded.mtime,seen_run=excluded.seen_run',
                        (target, os.path.basename(target), os.path.splitext(target)[1].lower(), '图片',
                         new_size, int(st.st_mtime), location_of(target), guess_cat(target, '图片'),
                         sub_of(target, '图片'), int(time.time() * 1000)))
            before += old_size; after += new_size
            done.append({'id': item_id, 'name': os.path.basename(target), 'old': old_size, 'new': new_size})
        except Exception as e:
            failed.append({'id': item_id, 'name': name, 'msg': '转换失败：' + str(e)})
    con.commit(); con.close()
    if done:
        bump_data()
    self._json({'ok': True, 'converted': len(done), 'failed': failed, 'before': before, 'after': after,
                'saved': max(0, before - after)})




# ---------------- 格式转换 ----------------
CONVERT_JOB = {'running': False, 'phase': 'idle', 'total': 0, 'done': 0, 'ok': 0, 'failed': 0,
               'msg': '', 'results': [], 'errors': [], 'started': 0, 'ended': 0}
CONVERT_LOCK = threading.Lock()


def _cjob(**kw):
    with CONVERT_LOCK:
        CONVERT_JOB.update(kw)


def convert_status():
    with CONVERT_LOCK:
        return dict(CONVERT_JOB)


def _index_output(path, media):
    """把转换出来的新文件写进索引，界面不用重启也能看到。"""
    try:
        st = os.stat(path)
        con = connect()
        con.execute('INSERT INTO items(path,name,ext,media,size,mtime,loc,cat,sub,seen_run)'
                    ' VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET size=excluded.size,'
                    ' mtime=excluded.mtime,seen_run=excluded.seen_run',
                    (path, os.path.basename(path), os.path.splitext(path)[1].lower(), media,
                     st.st_size, int(st.st_mtime), location_of(path), guess_cat(path, media),
                     sub_of(path, media), int(time.time() * 1000)))
        con.commit()
        con.close()
        return True
    except Exception:
        return False


def _ppt_to_pdf_direct(src, out):
    """真正调用本机 WPS / PowerPoint 的 COM 接口；只在独立子进程里跑，避免干扰主程序。"""
    try:
        import comtypes
        import comtypes.client
    except Exception:
        raise RuntimeError('缺少 comtypes 组件，无法调用本机 PowerPoint/WPS')
    comtypes.CoInitialize()
    app = None
    pres = None
    last = None
    try:
        for progid in ('KWPP.Application', 'PowerPoint.Application'):
            try:
                app = comtypes.client.CreateObject(progid, dynamic=True)
                # ReadOnly=True, Untitled=False, WithWindow=False：不弹窗，也不改动原文件
                pres = app.Presentations.Open(src, True, False, False)
                pres.SaveAs(out, 32)  # 32 = ppSaveAsPDF
                last = None
                break
            except Exception as e:
                last = e
                try:
                    if pres is not None:
                        pres.Close()
                except Exception:
                    pass
                pres = None
                try:
                    if app is not None:
                        app.Quit()
                except Exception:
                    pass
                app = None
    finally:
        try:
            if pres is not None:
                pres.Close()
        except Exception:
            pass
        try:
            if app is not None:
                app.Quit()
        except Exception:
            pass
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass
    if last is not None:
        raise RuntimeError('PPT 转 PDF 失败（本机需安装 WPS 或 PowerPoint）：' + str(last)[:180])
    if not os.path.exists(out) or os.path.getsize(out) <= 0:
        raise RuntimeError('PPT 导出后没有生成 PDF 文件')


def _ppt_to_pdf(src, out):
    """在主程序里用独立子进程调用 COM，失败不会拖垮素材坞，也不会留下 COM 状态。"""
    if os.environ.get('ASSETDOCK_PPT_CHILD') == '1':
        return _ppt_to_pdf_direct(src, out)
    log = out + '.log'
    try:
        if os.path.exists(log):
            os.remove(log)
    except Exception:
        pass
    if getattr(sys, 'frozen', False):
        cmd = [sys.executable, '--ppt2pdf', src, out]
    else:
        cmd = [sys.executable, os.path.abspath(__file__), '--ppt2pdf', src, out]
    env = os.environ.copy()
    env['ASSETDOCK_PPT_CHILD'] = '1'
    flags = 0
    if os.name == 'nt':
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
    try:
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=240,
                           creationflags=flags)
    except subprocess.TimeoutExpired:
        raise RuntimeError('PPT 转 PDF 超时（文件太大或 WPS/PowerPoint 没有响应）')
    if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
        try:
            os.remove(log)
        except Exception:
            pass
        return
    detail = ''
    try:
        if os.path.exists(log):
            with open(log, 'r', encoding='utf-8', errors='ignore') as f:
                detail = f.read().strip()
            os.remove(log)
    except Exception:
        pass
    if not detail:
        detail = (r.stderr or r.stdout or '').strip()[-240:]
    raise RuntimeError(detail or 'PPT 转 PDF 失败（本机需安装 WPS 或 PowerPoint）')


def _pdf_to_images(pdf_path, folder, base, fmt, dpi):
    import pypdfium2 as pdfium
    outs = []
    scale = max(72.0, min(600.0, float(dpi or 200))) / 72.0
    # pdfium 不是线程安全的，和封面渲染共用同一把锁
    with _PDF_LOCK:
        pdf = pdfium.PdfDocument(pdf_path)
        try:
            n = len(pdf)
            if n <= 0:
                raise RuntimeError('PDF 里没有页面')
            for i in range(n):
                page = pdf[i]
                try:
                    im = page.render(scale=scale).to_pil()
                    if fmt == 'jpg':
                        im = im.convert('RGB')
                        target = _unique_target(folder, '%s_第%02d页.jpg' % (base, i + 1))
                        im.save(target, 'JPEG', quality=92, optimize=True)
                    else:
                        target = _unique_target(folder, '%s_第%02d页.png' % (base, i + 1))
                        im.save(target, 'PNG', optimize=True)
                    outs.append(target)
                finally:
                    page.close()
        finally:
            pdf.close()
    return outs


def _flatten_rgb(im):
    """透明图转 JPG/PDF 时铺白底，避免透明区域变成黑色。"""
    from PIL import Image
    if ('A' in im.getbands()) or im.mode in ('P', 'LA'):
        rgba = im.convert('RGBA')
        bg = Image.new('RGB', rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.getchannel('A'))
        return bg
    return im.convert('RGB')


def _image_to_pdf(path, target):
    from PIL import Image, ImageOps
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        _flatten_rgb(im).save(target, 'PDF', resolution=150.0, quality=92)


def _images_to_pdf(paths, target):
    from PIL import Image, ImageOps
    imgs = []
    try:
        for p in paths:
            with Image.open(p) as im:
                imgs.append(_flatten_rgb(ImageOps.exif_transpose(im)).copy())
        imgs[0].save(target, 'PDF', save_all=True, append_images=imgs[1:],
                     resolution=150.0, quality=92)
    finally:
        for im in imgs:
            try:
                im.close()
            except Exception:
                pass


def _convert_one(path, name, ext, media, target, dpi, dest):
    """单个源文件转换，返回输出文件列表；失败抛异常。"""
    folder = dest or os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    base = os.path.splitext(name)[0]
    ext = (ext or '').lower()

    if ext in ('.ppt', '.pptx'):
        if target == 'pdf':
            out = _unique_target(folder, base + '.pdf')
            _ppt_to_pdf(path, out)
            return [out]
        tmpdir = tempfile.mkdtemp(prefix='assetdock_ppt_')
        try:
            tmp_pdf = os.path.join(tmpdir, 'slides.pdf')
            _ppt_to_pdf(path, tmp_pdf)
            return _pdf_to_images(tmp_pdf, folder, base, target, dpi)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    if ext == '.pdf':
        if target == 'pdf':
            raise RuntimeError('PDF 不需要再转 PDF')
        return _pdf_to_images(path, folder, base, target, dpi)

    if media == '图片':
        if target == 'pdf':
            out = _unique_target(folder, base + '.pdf')
            _image_to_pdf(path, out)
            return [out]
        from PIL import Image, ImageOps
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            if target == 'jpg':
                out = _unique_target(folder, base + '.jpg')
                _flatten_rgb(im).save(out, 'JPEG', quality=92, optimize=True)
            else:
                out = _unique_target(folder, base + '.png')
                if im.mode not in ('RGB', 'RGBA'):
                    im = im.convert('RGBA')
                im.save(out, 'PNG', optimize=True)
            return [out]

    raise RuntimeError('这个格式暂不支持转换')


def _convert_worker(rows, target, dpi, dest):
    total = len(rows)
    ok = failed = 0
    results, errors = [], []
    n_pre = 0
    try:
        # 多张图片转 PDF：合成一个文件，方便直接发出去
        if target == 'pdf':
            pics = [r for r in rows if r[4] == '图片']
            if len(pics) > 1:
                folder = dest or os.path.dirname(pics[0][1])
                os.makedirs(folder, exist_ok=True)
                name = '图片合集_' + time.strftime('%Y%m%d_%H%M%S') + '.pdf'
                out = _unique_target(folder, name)
                try:
                    _images_to_pdf([r[1] for r in pics], out)
                    _index_output(out, '文档')
                    ok += len(pics)
                    results.append({'id': 0, 'name': os.path.basename(out), 'path': out, 'n': len(pics)})
                except Exception as e:
                    failed += len(pics)
                    errors.append({'name': '图片合并 PDF', 'msg': str(e)[:180]})
                rows = [r for r in rows if r[4] != '图片']
                n_pre = len(pics)
                total = n_pre + len(rows)

        for idx, (item_id, path, name, ext, media) in enumerate(rows, 1):
            _cjob(msg='正在处理 %d/%d：%s' % (n_pre + idx, total, name), done=n_pre + idx - 1)
            try:
                outs = _convert_one(path, name, ext, media, target, dpi, dest)
                for out in outs:
                    _index_output(out, '图片' if out.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')) else '文档')
                ok += 1
                results.append({'id': item_id, 'name': name, 'outs': [os.path.basename(x) for x in outs],
                                'n': len(outs), 'path': outs[0] if outs else ''})
                if len(results) > 200:
                    results = results[-200:]
            except Exception as e:
                failed += 1
                errors.append({'name': name, 'msg': str(e)[:180]})
                if len(errors) > 200:
                    errors = errors[-200:]
        done = total
        msg = '完成：成功 %d 项，失败 %d 项' % (ok, failed)
        if errors:
            msg += '；' + errors[0]['msg']
        _cjob(running=False, phase='done', done=done, ok=ok, failed=failed, msg=msg,
              results=results, errors=errors, ended=time.time())
        if ok:
            bump_data()
    except Exception as e:
        _cjob(running=False, phase='error', msg='转换任务异常：' + str(e)[:200], ended=time.time())
    finally:
        # 兜底：异常路径也要释放 running，避免以后一直转不了
        if CONVERT_JOB.get('running'):
            _cjob(running=False, phase='error', msg='转换任务意外结束', ended=time.time())


def start_convert(ids, target, dpi=200, dest=''):
    target = (target or '').lower()
    if target not in ('pdf', 'png', 'jpg'):
        return False, '目标格式只支持 PDF / PNG / JPG'
    if isinstance(ids, (list, tuple)):
        try:
            ids = [int(x) for x in ids][:300]
        except (TypeError, ValueError):
            return False, '选中的文件编号不正确'
    else:
        ids = parse_ids(str(ids), 300)
    if not ids:
        return False, '先选中要转换的文件'
    dest = (dest or '').strip().strip('"')
    if dest and not os.path.isabs(dest):
        return False, '输出目录要填完整路径'
    with CONVERT_LOCK:
        if CONVERT_JOB.get('running'):
            return False, '已有一个转换任务在跑，等它完成再试'
        CONVERT_JOB.update({'running': True, 'phase': 'running', 'total': len(ids), 'done': 0,
                            'ok': 0, 'failed': 0, 'msg': '准备中…', 'results': [], 'errors': [],
                            'started': time.time(), 'ended': 0})
    con = connect()
    rows = []
    for i in ids:
        r = con.execute('SELECT id,path,name,ext,media FROM items WHERE id=?', (i,)).fetchone()
        if r and os.path.exists(r[1]):
            rows.append(r)
    con.close()
    if not rows:
        _cjob(running=False, phase='error', msg='选中的文件都已不在原位')
        return False, '选中的文件都已不在原位'
    t = threading.Thread(target=_convert_worker, args=(rows, target, dpi, dest), daemon=True)
    t.start()
    return True, '已开始转换 %d 项' % len(rows)


def watermark_remove_file(path, name, mask_s, radius=4):
    """按涂抹遮罩做本地 inpaint，返回新副本路径；不覆盖原图。"""
    import numpy as np
    import cv2
    from PIL import Image, ImageOps
    raw = mask_s.split(',', 1)[-1] if ',' in mask_s else mask_s
    mimg = Image.open(io.BytesIO(base64.b64decode(raw))).convert('L')
    with Image.open(path) as src:
        src = ImageOps.exif_transpose(src)
        has_alpha = ('A' in src.getbands()) or src.mode in ('P', 'LA')
        rgba = src.convert('RGBA') if has_alpha else None
        rgb = src.convert('RGB')
        w, h = rgb.size
        if mimg.size != (w, h):
            mimg = mimg.resize((w, h), Image.BILINEAR)
        m = np.array(mimg, dtype=np.uint8)
        m = np.where(m > 18, 255, 0).astype(np.uint8)
        if int(m.sum()) == 0:
            raise RuntimeError('还没有涂抹水印区域')
        radius = max(1, min(12, int(radius or 4)))
        if radius > 2:
            m = cv2.dilate(m, np.ones((3, 3), np.uint8), iterations=max(1, radius // 2))
        bgr = cv2.cvtColor(np.array(rgb), cv2.COLOR_RGB2BGR)
        fixed = cv2.inpaint(bgr, m, radius, cv2.INPAINT_TELEA)
        out_img = Image.fromarray(cv2.cvtColor(fixed, cv2.COLOR_BGR2RGB))
        if rgba is not None:
            out_img.putalpha(rgba.getchannel('A'))
        folder = os.path.dirname(path)
        base = os.path.splitext(name)[0]
        target = _unique_target(folder, base + '_去水印.png')
        out_img.save(target, 'PNG', optimize=True)
    return target


def _wm_auto_mask(rgb, strength=3):
    """自动找“半透明浅色/深色文字 + 角标”候选区，返回像素遮罩和候选框。

    只做保守检测：低饱和、局部对比强的细笔画，横向合并成文字行；找不到就明确失败，
    不用大面积涂抹冒充成功。复杂彩色 logo 可能识别不到，可提高识别强度再试。
    """
    import numpy as np
    import cv2
    h, w = rgb.shape[:2]
    scale = min(1.0, 1800.0 / max(1, max(h, w)))
    if scale < 1:
        small = cv2.resize(rgb, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
    else:
        small = rgb
    sh, sw = small.shape[:2]
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(small, cv2.COLOR_RGB2HSV)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    tophat = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, k)
    blackhat = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, k)
    contrast = cv2.max(tophat, blackhat)
    thr = (45, 36, 28, 21, 15)[max(1, min(5, int(strength))) - 1]
    # 半透明白字/深字：用局部均值差补足笔画内部（tophat 往往只抓到边缘，容易残留鬼影）
    sigma = max(5, min(sh, sw) / 45.0)
    local = cv2.GaussianBlur(gray, (0, 0), sigma)
    bright_diff = gray.astype(np.int16) - local.astype(np.int16)
    dark_diff = local.astype(np.int16) - gray.astype(np.int16)
    white_px = (hsv[:, :, 1] < 72) & (hsv[:, :, 2] > 150)
    dark_px = (hsv[:, :, 1] < 72) & (hsv[:, :, 2] < 105)
    cand = (((tophat > thr * 0.45) | (bright_diff > 9)) & white_px) | \
           (((blackhat > thr * 0.45) | (dark_diff > 9)) & dark_px)
    # 先在“笔画级”连通域上过滤，再按同一行/相近字高聚成文字块。
    # 直接横向膨胀会把文字和背景纹理连成巨大连通域，反而漏掉真正的水印。
    raw = cand.astype(np.uint8)
    n, labels, stats, cents = cv2.connectedComponentsWithStats(raw, 8)
    letters = []
    for i in range(1, n):
        x, y, bw, bh, area = [int(v) for v in stats[i]]
        if bw < 2 or bh < 2 or area < 8:
            continue
        if bw > sw * 0.60 or bh > sh * 0.60:
            continue
        fill = float(area) / max(1, bw * bh)
        if fill < 0.06:
            continue
        roi_c = contrast[y:y + bh, x:x + bw]
        roi_m = raw[y:y + bh, x:x + bw].astype(bool)
        sm = float(roi_c[roi_m].mean()) if roi_m.any() else 0.0
        score = sm * math.sqrt(max(1, area)) * (0.65 + 0.35 * fill)
        letters.append((score, x, y, bw, bh, area))
    letters.sort(key=lambda z: (z[2] + z[4] / 2, z[1]))
    used = [False] * len(letters)
    clusters = []
    for i, seed in enumerate(letters):
        if used[i]:
            continue
        used[i] = True
        stack = [i]
        group = [seed]
        while stack:
            cur = stack.pop()
            _, cx, cy, cw, ch, ca = letters[cur]
            for j, l in enumerate(letters):
                if used[j]:
                    continue
                _, x, y, bw, bh, area = l
                cy1 = cy + ch / 2
                cy2 = y + bh / 2
                gap_y = max(4, int(min(ch, bh) * 0.75))
                gap_x = max(10, int(max(ch, bh) * 1.35))
                if abs(cy1 - cy2) <= gap_y and not (x > cx + cw + gap_x or x + bw + gap_x < cx):
                    used[j] = True
                    stack.append(j)
                    group.append(l)
        gx = min(z[1] for z in group)
        gy = min(z[2] for z in group)
        gx2 = max(z[1] + z[3] for z in group)
        gy2 = max(z[2] + z[4] for z in group)
        bw, bh = gx2 - gx, gy2 - gy
        if bw < 18 or bh < 6:
            continue
        if bw > sw * 0.96 or bh > sh * 0.30:
            continue
        if bw / max(1, bh) < 1.5 or bw / max(1, bh) > 22:
            continue
        garea = sum(z[5] for z in group)
        fill = float(garea) / max(1, bw * bh)
        if fill < 0.10 or fill > 0.62:
            continue
        score = sum(z[0] for z in group) * (0.70 + 0.30 * fill)
        clusters.append((score, gx, gy, bw, bh))
    clusters.sort(reverse=True)
    # 只取最像水印的候选，并把同一行的邻近块再合并一次
    chosen = []
    for b in clusters[:36]:
        _, x, y, bw, bh = b
        merged = False
        for j, c in enumerate(chosen):
            cx, cy, cw, ch = c[1], c[2], c[3], c[4]
            gap = max(10, int(min(bh, ch) * 0.55))
            if not (x > cx + cw + gap or x + bw + gap < cx
                    or y > cy + ch + max(3, gap // 3) or y + bh + max(3, gap // 3) < cy):
                nx, ny = min(x, cx), min(y, cy)
                nx2, ny2 = max(x + bw, cx + cw), max(y + bh, cy + ch)
                chosen[j] = (max(c[0], b[0]), nx, ny, nx2 - nx, ny2 - ny)
                merged = True
                break
        if not merged:
            chosen.append(b)
        if len(chosen) >= 18:
            break
    mask_small = np.zeros((sh, sw), np.uint8)
    for _, x, y, bw, bh in chosen:
        # 用候选笔画的“文字行带”做整段填充：把字间空隙一起盖住，避免半透明字留下鬼影。
        roi = raw[y:y + bh, x:x + bw]
        rows = roi.sum(axis=1)
        cols = roi.sum(axis=0)
        thr_row = max(2, int(bw * 0.012))
        thr_col = max(1, int(bh * 0.02))
        ys = [i for i, v in enumerate(rows) if v >= thr_row]
        xs = [i for i, v in enumerate(cols) if v >= thr_col]
        if not ys or not xs:
            continue
        # 找连续行段，取候选像素最多的一段，避免把邻近背景边缘算进来
        runs, cur = [], [ys[0]]
        for v in ys[1:]:
            if v == cur[-1] + 1:
                cur.append(v)
            else:
                runs.append(cur); cur = [v]
        runs.append(cur)
        run = max(runs, key=lambda r: rows[r].sum())
        y1, y2 = y + run[0], y + run[-1] + 1
        x1, x2 = x + min(xs), x + max(xs) + 1
        mask_small[max(0, y1 - 2):min(sh, y2 + 2), max(0, x1 - 2):min(sw, x2 + 2)] = 255
    # 外扩盖住抗锯齿边缘
    dil = 1 + max(0, int(strength) - 3)
    mask_small = cv2.dilate(mask_small, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)), iterations=dil)
    if not mask_small.any():
        return np.zeros((h, w), np.uint8), []
    # 笔画外扩，盖住抗锯齿边缘；按强度微调
    dil = 2 + max(0, int(strength) - 3)
    mask_small = cv2.dilate(mask_small, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)), iterations=dil)
    if scale < 1:
        mask = cv2.resize(mask_small, (w, h), interpolation=cv2.INTER_NEAREST)
    else:
        mask = mask_small
    return mask, chosen


def watermark_auto_file(path, name, strength=3, radius=5):
    """一键去水印：自动识别候选区域后 inpaint，生成新副本，不覆盖原图。"""
    import numpy as np
    import cv2
    from PIL import Image, ImageOps
    with Image.open(path) as src:
        src = ImageOps.exif_transpose(src)
        has_alpha = ('A' in src.getbands()) or src.mode in ('P', 'LA')
        rgba = src.convert('RGBA') if has_alpha else None
        rgb = src.convert('RGB')
        arr = np.array(rgb)
        mask, boxes = _wm_auto_mask(arr, strength)
        if not mask.any():
            raise RuntimeError('没有识别到明显水印，可把识别强度调高再试')
        ratio = float((mask > 0).sum()) / max(1, mask.size)
        if ratio > 0.22:
            raise RuntimeError('识别区域过大（%.0f%%），可能误伤画面，请把强度调低' % (ratio * 100))
        radius = max(2, min(10, int(radius or 5)))
        bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        fixed = cv2.inpaint(bgr, mask, radius, cv2.INPAINT_TELEA)
        out_img = Image.fromarray(cv2.cvtColor(fixed, cv2.COLOR_BGR2RGB))
        if rgba is not None:
            out_img.putalpha(rgba.getchannel('A'))
        folder = os.path.dirname(path)
        base = os.path.splitext(name)[0]
        target = _unique_target(folder, base + '_去水印.png')
        out_img.save(target, 'PNG', optimize=True)
    return target, int((mask > 0).sum()), len(boxes)



DISCOVER_KIND = {'image': '图片', 'video': '视频', 'audio': '音频', 'doc': '文档'}
DISCOVER_DL_IMAGE = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.tif', '.tiff', '.avif'}
DISCOVER_DL_VIDEO = {'.mp4', '.webm', '.mov', '.mkv', '.avi', '.m4v', '.flv'}
DISCOVER_DL_AUDIO = {'.mp3', '.wav', '.m4a', '.ogg', '.flac', '.aac', '.opus'}
DISCOVER_DL_DOC = {'.pdf', '.ppt', '.pptx'}
DISCOVER_SAFE_EXT = DISCOVER_DL_IMAGE | DISCOVER_DL_VIDEO | DISCOVER_DL_AUDIO | DISCOVER_DL_DOC
DISCOVER_CTYPE_EXT = {
    'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp', 'image/gif': '.gif',
    'image/bmp': '.bmp', 'image/tiff': '.tif', 'image/avif': '.avif',
    'video/mp4': '.mp4', 'video/webm': '.webm', 'video/quicktime': '.mov',
    'video/x-matroska': '.mkv', 'video/x-msvideo': '.avi',
    'audio/mpeg': '.mp3', 'audio/wav': '.wav', 'audio/x-wav': '.wav',
    'audio/mp4': '.m4a', 'audio/ogg': '.ogg', 'audio/flac': '.flac',
    'application/pdf': '.pdf',
}

DISCOVER_SOURCES = [
    {'key': 'bing', 'name': 'Bing 图片', 'kinds': ['图片'],
     'note': '搜索引擎结果，覆盖广；版权状态未知，下载前请自行确认授权'},
    {'key': 'arxiv', 'name': 'arXiv', 'kinds': ['文档'],
     'note': '公开论文预印本，可直接下载 PDF'},
    {'key': 'openalex', 'name': 'OpenAlex', 'kinds': ['文档'],
     'note': '开放获取论文，优先取 PDF 直链'},
]


def _clean_discover_quarantine():
    """隔离区超过 24 小时的残留下载自动清掉，避免占空间。"""
    try:
        now = time.time()
        for ent in os.scandir(DISCOVER_QUAR):
            if ent.is_dir() and now - ent.stat().st_mtime > 86400:
                shutil.rmtree(ent.path, ignore_errors=True)
    except Exception:
        pass


def _discover_job(**kw):
    with DISCOVER_LOCK:
        DISCOVER_JOB.update(kw)


def discover_status():
    with DISCOVER_LOCK:
        return dict(DISCOVER_JOB)


def _host_private(host):
    h = (host or '').strip().strip('[]').lower().rstrip('.')
    if not h:
        return True
    if h in ('localhost', 'localhost.localdomain') or h.endswith('.local') or h.endswith('.localhost'):
        return True
    try:
        ip = ipaddress.ip_address(h)
        return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                    or ip.is_multicast or ip.is_unspecified)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(h, None)
    except Exception:
        return True
    for fam, _, _, _, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except Exception:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return True
    return False


def _safe_remote_url(url, allow_private=None):
    """只允许 http/https 公网地址，挡住 localhost / 内网 / 云元数据地址（SSRF）。"""
    u = (url or '').strip()
    if not u:
        raise ValueError('缺少资源地址')
    if u.startswith('//'):
        u = 'https:' + u
    if not re.match(r'^https?://', u, re.I):
        u = 'https://' + u.lstrip('/')
    p = urlparse(u)
    if p.scheme.lower() not in ('http', 'https'):
        raise ValueError('只支持 http / https 地址')
    if not p.hostname:
        raise ValueError('地址缺少域名')
    if p.username or p.password:
        raise ValueError('地址里不允许带账号密码')
    ap = ALLOW_PRIVATE if allow_private is None else allow_private
    if not ap and _host_private(p.hostname):
        raise ValueError('已拦截内网 / 本机地址')
    return u


class _DiscoverRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newurl = urljoin(req.full_url, newurl)
        _safe_remote_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_remote(url, timeout=18, headers=None):
    url = _safe_remote_url(url)
    h = {'User-Agent': DISCOVER_UA, 'Accept': '*/*', 'Accept-Encoding': 'identity'}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    ctx = ssl.create_default_context(cafile=_CA_BUNDLE or None)
    opener = urllib.request.build_opener(_DiscoverRedirect(), urllib.request.HTTPSHandler(context=ctx))
    return opener.open(req, timeout=timeout)


def _read_limited(resp, limit):
    chunks = []
    total = 0
    while True:
        b = resp.read(65536)
        if not b:
            break
        total += len(b)
        if total > limit:
            raise ValueError('内容超过 %d MB 上限' % (limit // 1048576))
        chunks.append(b)
    return b''.join(chunks)


def _fetch_remote(url, timeout=18, limit=DISCOVER_MAX_HTML, headers=None):
    with _open_remote(url, timeout=timeout, headers=headers) as r:
        final = r.geturl()
        _safe_remote_url(final)
        ctype = (r.headers.get('Content-Type') or '').split(';')[0].strip().lower()
        data = _read_limited(r, limit)
        return data, ctype, final, dict(r.headers)


def _fetch_json(url, timeout=18, limit=DISCOVER_MAX_HTML):
    data, ctype, final, headers = _fetch_remote(url, timeout=timeout, limit=limit)
    try:
        return json.loads(data.decode('utf-8', 'replace')), final, ctype
    except Exception as e:
        raise ValueError('返回的不是有效 JSON：%s' % e)


def _thumb_cache(url):
    key = hashlib.sha1(url.encode('utf-8')).hexdigest()
    p = os.path.join(DISCOVER_THUMB, key + '.png')
    try:
        if os.path.exists(p) and time.time() - os.path.getmtime(p) < 7 * 86400:
            return p
    except Exception:
        pass
    return p


def _make_thumb_bytes(url):
    os.makedirs(DISCOVER_THUMB, exist_ok=True)
    data, ctype, final, headers = _fetch_remote(url, timeout=15, limit=DISCOVER_MAX_THUMB,
                                                 headers={'Accept': 'image/*'})
    if not (ctype.startswith('image/') or ctype == 'application/octet-stream'):
        raise ValueError('预览不是图片')
    if ctype == 'image/svg+xml' or url.lower().split('?')[0].endswith('.svg'):
        raise ValueError('不预览 SVG')
    try:
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(data)) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((1400, 1400), Image.LANCZOS)
            if im.mode not in ('RGB', 'RGBA'):
                im = im.convert('RGBA' if 'A' in im.getbands() else 'RGB')
            buf = io.BytesIO()
            im.save(buf, 'PNG', optimize=True)
            return buf.getvalue()
    except Exception as e:
        raise ValueError('图片解不出来：%s' % str(e)[:80])


def _card(**kw):
    d = {'url': '', 'name': '', 'kind': '图片', 'source': '', 'license': '版权未知',
         'author': '', 'page': '', 'thumb': '', 'desc': ''}
    d.update(kw)
    return d


def _basename_from_url(url, fallback='资源'):
    try:
        path = urllib.parse.unquote(urlparse(url).path)
        nm = os.path.basename(path)
        if nm and len(nm) <= 160:
            return nm
    except Exception:
        pass
    return fallback


def _bing_search(q, limit=16):
    url = 'https://cn.bing.com/images/async?' + urlencode({
        'q': q, 'first': 1, 'count': max(12, min(35, limit * 2)), 'mmasync': 1, 'mkt': 'zh-CN'})
    data, ctype, final, headers = _fetch_remote(url, timeout=16, limit=5 * 1024 * 1024,
                                                headers={'Accept': 'text/html,*/*'})
    text = data.decode('utf-8', 'replace')
    out, seen = [], set()
    for m in re.finditer(r'm="([^"]+)"', text):
        raw = htmllib.unescape(m.group(1))
        try:
            o = json.loads(raw)
        except Exception:
            continue
        murl = (o.get('murl') or '').strip()
        turl = (o.get('turl') or '').strip()
        purl = (o.get('purl') or '').strip()
        if not (murl.startswith('http://') or murl.startswith('https://')):
            continue
        if murl in seen:
            continue
        seen.add(murl)
        title = (o.get('t') or '').strip() or _basename_from_url(murl, '图片')
        out.append(_card(url=murl, name=title[:120], kind='图片', source='Bing 图片',
                         license='版权未知（搜索引擎结果）', page=purl, thumb=turl,
                         desc='来自 Bing 图片搜索，原图直链可能受站点防盗链限制'))
        if len(out) >= limit:
            break
    return out


_ATOM = '{http://www.w3.org/2005/Atom}'


def _arxiv_search(q, limit=12):
    url = 'https://export.arxiv.org/api/query?' + urlencode({
        'search_query': 'all:' + q, 'start': 0, 'max_results': max(1, min(20, limit))})
    data, ctype, final, headers = _fetch_remote(url, timeout=20, limit=5 * 1024 * 1024)
    root = ET.fromstring(data)
    out = []
    for e in root.findall(_ATOM + 'entry'):
        title = (e.findtext(_ATOM + 'title') or '').strip().replace('\n', ' ')
        aid = (e.findtext(_ATOM + 'id') or '').strip()
        authors = []
        for a in e.findall(_ATOM + 'author'):
            nm = (a.findtext(_ATOM + 'name') or '').strip()
            if nm:
                authors.append(nm)
        pdf = ''
        for ln in e.findall(_ATOM + 'link'):
            if (ln.get('type') or '').lower() == 'application/pdf' or (ln.get('title') or '').lower() == 'pdf':
                pdf = ln.get('href') or ''
                break
        if not pdf:
            continue
        nm = re.sub(r'[\\/:*?"<>|]', '_', title)[:100].strip() or '论文'
        out.append(_card(url=pdf, name=nm + '.pdf', kind='文档', source='arXiv',
                         license='arXiv（按论文许可）', author='、'.join(authors[:3]),
                         page=aid, desc=(e.findtext(_ATOM + 'summary') or '').strip().replace('\n', ' ')[:220]))
        if len(out) >= limit:
            break
    return out


def _openalex_search(q, limit=12):
    url = 'https://api.openalex.org/works?' + urlencode({
        'search': q, 'per-page': max(1, min(20, limit)), 'filter': 'is_oa:true'})
    data, final, ctype = _fetch_json(url, timeout=20, limit=5 * 1024 * 1024)
    out = []
    for w in (data.get('results') or []):
        title = (w.get('display_name') or '').strip()
        best = w.get('best_oa_location') or {}
        pdf = (best.get('pdf_url') or '').strip()
        if not pdf:
            continue
        page = (best.get('landing_page_url') or (w.get('primary_location') or {}).get('landing_page_url')
                or (w.get('open_access') or {}).get('oa_url') or '')
        authors = []
        for a in (w.get('authorships') or [])[:3]:
            nm = ((a.get('author') or {}).get('display_name') or '').strip()
            if nm:
                authors.append(nm)
        lic = (best.get('license') or (w.get('open_access') or {}).get('oa_status') or '开放获取')
        nm = re.sub(r'[\\/:*?"<>|]', '_', title)[:100].strip() or '论文'
        out.append(_card(url=pdf, name=nm + '.pdf', kind='文档', source='OpenAlex',
                         license=lic, author='、'.join(authors), page=page,
                         desc='开放获取论文，PDF 由 OpenAlex 指向'))
        if len(out) >= limit:
            break
    return out


def _search_one_source(key, q, limit):
    if key == 'bing':
        return _bing_search(q, limit)
    if key == 'arxiv':
        return _arxiv_search(q, limit)
    if key == 'openalex':
        return _openalex_search(q, limit)
    return []


def discover_sources():
    return {'sources': DISCOVER_SOURCES, 'kinds': [{'key': k, 'name': v} for k, v in DISCOVER_KIND.items()],
            'note': 'Google 在当前网络不可达，未作为默认源；Bing 图片可选，版权需自行确认。'}


def discover_search(q, kinds='', sources='', limit=16):
    q = (q or '').strip()
    if not q:
        return {'ok': False, 'msg': '请输入关键词', 'items': [], 'status': []}
    if len(q) > 80:
        q = q[:80]
    kind_names = set()
    for k in (kinds or '').split(','):
        k = k.strip()
        if k in DISCOVER_KIND:
            kind_names.add(DISCOVER_KIND[k])
        elif k in DISCOVER_KIND.values():
            kind_names.add(k)
    if not kind_names:
        kind_names = set(DISCOVER_KIND.values())
    want = [s.strip() for s in (sources or '').split(',') if s.strip()]
    if not want:
        want = [x['key'] for x in DISCOVER_SOURCES]
    try:
        limit = max(4, min(32, int(limit or 16)))
    except Exception:
        limit = 16

    def run(src):
        t0 = time.time()
        if not set(src['kinds']) & kind_names:
            return src['name'], [], '跳过（与所选类型不匹配）', 0
        try:
            rows = _search_one_source(src['key'], q, limit)
            return src['name'], rows, '', time.time() - t0
        except Exception as e:
            return src['name'], [], str(e)[:160], time.time() - t0

    selected = [s for s in DISCOVER_SOURCES if s['key'] in want]
    status, rows = [], []
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, min(3, len(selected)))) as ex:
        futs = [ex.submit(run, s) for s in selected]
        for f in futs:
            nm, rs, err, sec = f.result()
            status.append({'source': nm, 'count': len(rs), 'error': err, 'seconds': round(sec, 1)})
            rows.extend(rs)
    seen, uniq = set(), []
    for r in rows:
        key = (r.get('url') or '').strip()
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    uniq = uniq[:96]
    return {'ok': True, 'items': uniq, 'status': status, 'q': q, 'total': len(uniq),
            'note': '只搜公开可访问资源；下载先入隔离区，校验和扫描通过后再入库。'}


class _SiteHTML(HTMLParser):
    """只解析公开网页里的媒体地址和同域链接，不执行脚本、不加载页面资源。"""

    def __init__(self, page_url):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.title = ''
        self._in_title = False
        self.media = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or '') for k, v in attrs}
        if tag == 'title':
            self._in_title = True
        if tag == 'meta':
            prop = (a.get('property') or a.get('name') or '').lower()
            content = (a.get('content') or '').strip()
            km = {'og:image': '图片', 'og:video': '视频', 'og:audio': '音频', 'twitter:image': '图片'}
            if prop in km and content:
                self.media.append({'url': urljoin(self.page_url, content), 'name': '', 'kind': km[prop]})
        if tag in ('img', 'video', 'audio', 'source'):
            src = (a.get('src') or a.get('data-src') or a.get('data-original') or '').strip()
            if src and not src.startswith(('data:', 'javascript:', 'blob:')):
                kind = '视频' if tag in ('video', 'source') else ('音频' if tag == 'audio' else '图片')
                self.media.append({'url': urljoin(self.page_url, src),
                                   'name': (a.get('alt') or a.get('title') or '').strip(), 'kind': kind})
            srcset = (a.get('srcset') or a.get('data-srcset') or '').strip()
            if srcset:
                first = srcset.split(',')[0].strip().split(' ')[0]
                if first and not first.startswith(('data:', 'javascript:', 'blob:')):
                    self.media.append({'url': urljoin(self.page_url, first),
                                       'name': (a.get('alt') or a.get('title') or '').strip(), 'kind': '图片'})
        if tag == 'a':
            href = (a.get('href') or '').strip()
            if href and not href.startswith(('javascript:', '#', 'mailto:', 'tel:')):
                self.links.append(urljoin(self.page_url, href))

    def handle_endtag(self, tag):
        if tag == 'title':
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and data.strip():
            self.title = (self.title + ' ' + data.strip()).strip()[:200]


def _kind_from_url(url):
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext in DISCOVER_DL_IMAGE:
        return '图片'
    if ext in DISCOVER_DL_VIDEO:
        return '视频'
    if ext in DISCOVER_DL_AUDIO:
        return '音频'
    if ext in DISCOVER_DL_DOC:
        return '文档'
    return ''


def _site_robots_allowed(base, page_url):
    try:
        robots = base.rstrip('/') + '/robots.txt'
        data, ctype, final, headers = _fetch_remote(robots, timeout=8, limit=512 * 1024,
                                                    headers={'Accept': 'text/plain,*/*'})
        text = data.decode('utf-8', 'replace')
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(text.splitlines())
        return rp.can_fetch(DISCOVER_UA, page_url), ''
    except Exception as e:
        # robots 取不到时按“允许但限速”处理；真正禁止抓取的站点通常会在页面上挑战或返回 403
        return True, 'robots.txt 读取失败，按限速抓取：%s' % str(e)[:60]


def discover_site(url, q='', kinds='', max_pages=5, max_items=80, allow_private=None):
    u = (url or '').strip()
    if not u:
        return {'ok': False, 'msg': '请输入站点地址', 'items': [], 'pages': 0}
    try:
        u = _safe_remote_url(u, allow_private=allow_private)
    except Exception as e:
        return {'ok': False, 'msg': str(e), 'items': [], 'pages': 0}
    q = (q or '').strip()[:80]
    kind_names = set()
    for k in (kinds or '').split(','):
        if k in DISCOVER_KIND:
            kind_names.add(DISCOVER_KIND[k])
        elif k in DISCOVER_KIND.values():
            kind_names.add(k)
    if not kind_names:
        kind_names = set(DISCOVER_KIND.values())
    try:
        max_pages = max(1, min(10, int(max_pages or 5)))
        max_items = max(5, min(150, int(max_items or 80)))
    except Exception:
        max_pages, max_items = 5, 80

    # 用户直接贴了一张图片/视频/PDF 直链：不再抓页面，直接作为单条结果
    direct_kind = _kind_from_url(u)
    if direct_kind:
        return {'ok': True, 'items': [_card(url=u, name=_basename_from_url(u, direct_kind),
                                            kind=direct_kind, source='站点嗅探',
                                            license='未知（需确认授权）', page=u,
                                            thumb=u if direct_kind == '图片' else '')],
                'pages': 0, 'title': '', 'note': '这是一个资源直链，可直接下载并校验。'}

    host = urlparse(u).hostname
    base = '%s://%s' % (urlparse(u).scheme, urlparse(u).netloc)
    allowed, rwarn = _site_robots_allowed(base, u)
    if not allowed:
        return {'ok': False, 'msg': '该站点 robots.txt 禁止抓取这个页面', 'items': [], 'pages': 0,
                'warnings': [rwarn] if rwarn else []}

    seen_pages, seen_media, items, warnings = set(), set(), [], []
    queue = [(u, 0)]
    pages = 0
    while queue and pages < max_pages and len(items) < max_items:
        page_url, depth = queue.pop(0)
        pkey = page_url.split('#')[0]
        if pkey in seen_pages:
            continue
        seen_pages.add(pkey)
        try:
            data, ctype, final, headers = _fetch_remote(
                page_url, timeout=16, limit=DISCOVER_MAX_HTML, headers={'Accept': 'text/html,*/*'})
            if ctype and not (ctype.startswith('text/html') or ctype.startswith('application/xhtml')):
                if _kind_from_url(final):
                    items.append(_card(url=final, name=_basename_from_url(final, '资源'),
                                       kind=_kind_from_url(final), source='站点嗅探',
                                       license='未知（需确认授权）', page=final,
                                       thumb=final if _kind_from_url(final) == '图片' else ''))
                break
            parser = _SiteHTML(final)
            parser.feed(data.decode('utf-8', 'replace'))
            pages += 1
            page_title = parser.title or (urlparse(final).hostname or '')
            media = list(parser.media)
            for link in parser.links:
                lk = link.split('#')[0]
                if _kind_from_url(lk) in DISCOVER_KIND.values():
                    media.append({'url': lk, 'name': '', 'kind': _kind_from_url(lk)})
            for m in media:
                mu = (m.get('url') or '').split('#')[0]
                if not mu or mu in seen_media:
                    continue
                mk = m.get('kind') or _kind_from_url(mu)
                if mk not in kind_names:
                    continue
                nm = (m.get('name') or '').strip() or _basename_from_url(mu, mk)
                if q and q.lower() not in (mu + ' ' + nm + ' ' + page_title).lower():
                    continue
                seen_media.add(mu)
                items.append(_card(url=mu, name=nm[:120], kind=mk, source='站点嗅探',
                                   license='未知（个人站点，使用前需作者授权）', page=final,
                                   thumb=mu if mk == '图片' else '', desc=page_title[:180]))
                if len(items) >= max_items:
                    break
            # 只跟同域、看起来像页面的链接；媒体链接已经在上面收集
            if depth < 2 and pages < max_pages:
                for link in parser.links:
                    lk = link.split('#')[0]
                    if not lk.startswith(('http://', 'https://')):
                        continue
                    lp = urlparse(lk)
                    if lp.hostname != host or _kind_from_url(lk):
                        continue
                    ext = os.path.splitext(lp.path)[1].lower()
                    if ext and ext not in ('.html', '.htm', '.shtml', '.php', '.asp', '.aspx'):
                        continue
                    if lk not in seen_pages and (lk, depth + 1) not in queue:
                        if len(queue) < max_pages * 3:
                            queue.append((lk, depth + 1))
            if queue:
                time.sleep(0.35)
        except Exception as e:
            warnings.append('%s：%s' % (page_url[:80], str(e)[:100]))
            continue

    if not items:
        msg = ('该站点前 %d 页没有找到可下载的公开资源' % pages)
        if q:
            msg += '，已按关键词「%s」过滤；可试试不带关键词' % q
        return {'ok': True, 'items': [], 'pages': pages, 'title': '', 'msg': msg,
                'warnings': warnings[:8], 'note': '站点嗅探只读公开页面，不绕过登录/验证码/防盗链。'}
    return {'ok': True, 'items': items, 'pages': pages, 'title': '', 'warnings': warnings[:8],
            'note': '只提取页面里公开的媒体直链；个人站点资源默认有版权，使用前请确认授权。'}


# ---------- 受控下载：隔离区 + 魔数校验 + Defender ----------

def _discover_name(name, ctype, url, kind):
    nm = (name or '').strip()
    nm = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', nm).strip(' .')
    if not nm:
        nm = _basename_from_url(url, DISCOVER_KIND.get(kind, '资源'))
        nm = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', nm)
    if len(nm) > 110:
        stem, ext = os.path.splitext(nm)
        nm = stem[:100] + ext[:10]
    ext = os.path.splitext(nm)[1].lower()
    if ext not in DISCOVER_SAFE_EXT:
        ext = _discover_ext(ctype, url)
        if ext:
            nm = os.path.splitext(nm)[0] + ext
    return nm


def _discover_ext(ctype, url):
    ctype = (ctype or '').lower()
    if ctype in DISCOVER_CTYPE_EXT:
        return DISCOVER_CTYPE_EXT[ctype]
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext in DISCOVER_SAFE_EXT:
        return ext
    return ''


def _discover_magic_ok(ext, head):
    ext = ext.lower()
    h = head or b''
    if ext in DISCOVER_DL_IMAGE:
        if ext in ('.jpg', '.jpeg'):
            return h.startswith(b'\xff\xd8\xff')
        if ext == '.png':
            return h.startswith(b'\x89PNG\r\n\x1a\n')
        if ext == '.gif':
            return h.startswith((b'GIF87a', b'GIF89a'))
        if ext == '.webp':
            return h[:4] == b'RIFF' and h[8:12] == b'WEBP'
        if ext == '.bmp':
            return h.startswith(b'BM')
        if ext in ('.tif', '.tiff'):
            return h.startswith((b'II*\x00', b'MM\x00*'))
        if ext == '.avif':
            return b'ftyp' in h[:32] and (b'avif' in h[:64] or b'avis' in h[:64])
        return False
    if ext == '.pdf':
        return h.startswith(b'%PDF-')
    if ext in ('.ppt', '.doc'):
        return h.startswith(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1')
    if ext in ('.pptx', '.docx', '.xlsx'):
        return h.startswith(b'PK\x03\x04')
    if ext in DISCOVER_DL_VIDEO:
        if ext in ('.mp4', '.m4v', '.mov'):
            return b'ftyp' in h[:32]
        if ext in ('.webm', '.mkv'):
            return h.startswith(b'\x1aE\xdf\xa3')
        if ext == '.avi':
            return h[:4] == b'RIFF' and b'AVI' in h[8:16]
        if ext == '.flv':
            return h.startswith(b'FLV')
        return False
    if ext in DISCOVER_DL_AUDIO:
        if ext == '.mp3':
            return h.startswith(b'ID3') or h[:2] in (b'\xff\xfb', b'\xff\xf3', b'\xff\xf2')
        if ext == '.wav':
            return h[:4] == b'RIFF' and h[8:12] == b'WAVE'
        if ext == '.ogg' or ext == '.opus':
            return h.startswith(b'OggS')
        if ext == '.flac':
            return h.startswith(b'fLaC')
        if ext == '.m4a' or ext == '.aac':
            return b'ftyp' in h[:32] or h[:2] in (b'\xff\xf1', b'\xff\xf9')
        return False
    return False


def _defender_scan(path):
    """调用 Windows Defender 命令行扫描；没装/找不到就返回 unavailable，不假装安全。"""
    exes = []
    try:
        exes += glob.glob(os.path.join(os.environ.get('ProgramData', r'C:\ProgramData'),
                                       'Microsoft', 'Windows Defender', 'Platform', '*', 'MpCmdRun.exe'))
    except Exception:
        pass
    for p in (r'C:\Program Files\Windows Defender\MpCmdRun.exe',
              r'C:\Program Files (x86)\Windows Defender\MpCmdRun.exe'):
        if os.path.exists(p):
            exes.append(p)
    if not exes:
        return 'unavailable', '未找到 Windows Defender 命令行'
    exe = max(exes, key=lambda p: os.path.getmtime(p))
    try:
        p = subprocess.run([exe, '-Scan', '-ScanType', '3', '-File', path, '-DisableRemediation'],
                           capture_output=True, text=True, timeout=120, creationflags=0x08000000)
        out = ((p.stdout or '') + (p.stderr or '')).strip()
        low = out.lower()
        if p.returncode == 0:
            return 'clean', out[-200:]
        if 'product/feature disabled' in low or 'feature disabled' in low:
            return 'unavailable', 'Windows Defender 功能未启用'
        if p.returncode == 2 and ('threat' in low or 'found' in low or 'detected' in low
                                  or '检测到' in out or '威胁' in out):
            return 'threat', out[-300:]
        return 'error', ('Defender 返回码 %s：%s' % (p.returncode, out[-200:]))
    except subprocess.TimeoutExpired:
        return 'error', 'Defender 扫描超时'
    except Exception as e:
        return 'error', str(e)[:160]


def _discover_download_worker(token, meta):
    qdir = os.path.join(DISCOVER_QUAR, token)
    os.makedirs(qdir, exist_ok=True)
    part = os.path.join(qdir, 'download.part')
    try:
        _discover_job(phase='connecting', msg='正在连接资源地址…', progress=3)
        url = _safe_remote_url(meta.get('url'))
        with _open_remote(url, timeout=25, headers={'Accept': '*/*'}) as r:
            final = r.geturl()
            _safe_remote_url(final)
            ctype = (r.headers.get('Content-Type') or '').split(';')[0].strip().lower()
            length = r.headers.get('Content-Length')
            if ctype.startswith(('text/', 'application/json', 'application/javascript', 'image/svg')):
                raise ValueError('目标返回的是网页/脚本，不是资源本体：%s' % ctype)
            size = 0
            with open(part, 'wb') as f:
                while True:
                    b = r.read(65536)
                    if not b:
                        break
                    size += len(b)
                    if size > DISCOVER_MAX_DL:
                        raise ValueError('文件超过 %d MB 上限' % (DISCOVER_MAX_DL // 1048576))
                    f.write(b)
                    if length:
                        try:
                            pct = min(92, 3 + int(size / max(1, int(length)) * 86))
                            _discover_job(progress=pct, msg='正在下载 %s / %s' % (
                                _human_size(size), _human_size(int(length))))
                        except Exception:
                            pass
                    elif size // 1048576:
                        _discover_job(progress=min(92, 3 + size // (2 * 1048576)),
                                      msg='正在下载 %s' % _human_size(size))
        if not os.path.exists(part) or os.path.getsize(part) == 0:
            raise ValueError('下载为空文件')
        head = open(part, 'rb').read(64)
        ext = _discover_ext(ctype, final or url)
        if not ext:
            raise ValueError('无法判断文件类型（Content-Type：%s）' % (ctype or '未知'))
        if not _discover_magic_ok(ext, head):
            raise ValueError('文件头与扩展名 %s 不符，已拒绝' % ext)
        name = _discover_name(meta.get('name'), ctype, final or url, meta.get('kind'))
        if os.path.splitext(name)[1].lower() != ext:
            name = os.path.splitext(name)[0] + ext
        size = os.path.getsize(part)
        sha = hashlib.sha256(open(part, 'rb').read()).hexdigest() if size < 64 * 1024 * 1024 else ''
        final_path = os.path.join(qdir, name)
        if os.path.exists(final_path):
            final_path = _unique_target(qdir, name)
        os.replace(part, final_path)
        _discover_job(phase='scanning', progress=94, msg='文件已写入隔离区，正在用 Defender 扫描…')
        scan, scan_msg = _defender_scan(final_path)
        if scan == 'threat':
            try:
                os.remove(final_path)
                os.rmdir(qdir)
            except Exception:
                pass
            raise ValueError('Defender 检出威胁，已删除隔离文件：%s' % scan_msg)
        media = media_of(os.path.splitext(final_path)[1]) or DISCOVER_KIND.get(meta.get('kind'), '文档')
        result = {'token': token, 'path': final_path, 'name': os.path.basename(final_path),
                  'size': size, 'ctype': ctype, 'sha256': sha, 'scan': scan,
                  'scan_msg': scan_msg[:180], 'media': media,
                  'source': meta.get('source') or '', 'license': meta.get('license') or '',
                  'page': meta.get('page') or '', 'title': meta.get('title') or meta.get('name') or '',
                  'url': final}
        _discover_job(phase='ready', progress=100, running=False,
                      msg='下载完成，已通过文件头校验' + ('，Defender 未检出威胁' if scan == 'clean' else '，但未执行 Defender 扫描'),
                      result=result)
    except Exception as e:
        try:
            if os.path.exists(part):
                os.remove(part)
            if os.path.isdir(qdir) and not os.listdir(qdir):
                os.rmdir(qdir)
        except Exception:
            pass
        _discover_job(phase='error', progress=100, running=False, error=str(e)[:240],
                      msg='下载失败：' + str(e)[:200])


def _human_size(n):
    try:
        n = float(n)
    except Exception:
        return ''
    if n >= 1048576:
        return '%.1f MB' % (n / 1048576)
    return '%.0f KB' % (n / 1024)


def start_discover_download(meta):
    with DISCOVER_LOCK:
        if DISCOVER_JOB.get('running'):
            return False, '已有下载任务在跑，请等它结束'
        token = uuid.uuid4().hex[:16]
        DISCOVER_JOB.update({'running': True, 'phase': 'starting', 'msg': '准备下载…',
                             'progress': 0, 'result': None, 'error': '', 'started': time.time()})
    t = threading.Thread(target=_discover_download_worker, args=(token, dict(meta)), daemon=True)
    t.start()
    return True, token


def discover_ingest(token):
    token = (token or '').strip()
    if not re.match(r'^[0-9a-f]{16}$', token):
        return {'ok': False, 'msg': '下载凭据无效'}
    qdir = os.path.realpath(os.path.join(DISCOVER_QUAR, token))
    root = os.path.realpath(DISCOVER_QUAR)
    if os.path.commonpath([qdir, root]) != root or not os.path.isdir(qdir):
        return {'ok': False, 'msg': '隔离目录不存在或已过期'}
    files = [os.path.join(qdir, f) for f in os.listdir(qdir)
             if os.path.isfile(os.path.join(qdir, f)) and not f.endswith('.part')]
    if not files:
        return {'ok': False, 'msg': '隔离区里没有可入库的文件'}
    src = files[0]
    name = os.path.basename(src)
    media = media_of(os.path.splitext(name)[1])
    if not media:
        return {'ok': False, 'msg': '这个文件类型不能入库：' + os.path.splitext(name)[1]}
    target = _unique_target(DISCOVER_DIR, name)
    try:
        shutil.move(src, target)
        ok = _index_output(target, media)
        bump_data()
        try:
            shutil.rmtree(qdir, ignore_errors=True)
        except Exception:
            pass
        item_id = 0
        try:
            con = connect()
            r = con.execute('SELECT id FROM items WHERE path=?', (target,)).fetchone()
            con.close()
            item_id = r[0] if r else 0
        except Exception:
            pass
        return {'ok': True, 'path': target, 'name': os.path.basename(target), 'id': item_id,
                'indexed': bool(ok), 'media': media, 'msg': '已入库'}
    except Exception as e:
        return {'ok': False, 'msg': '入库失败：' + str(e)[:180]}


def discover_thumb_path(url):
    url = (url or '').strip()
    if not url:
        raise ValueError('缺少预览地址')
    os.makedirs(DISCOVER_THUMB, exist_ok=True)
    p = _thumb_cache(url)
    if os.path.exists(p):
        return p
    data = _make_thumb_bytes(url)
    with open(p, 'wb') as f:
        f.write(data)
    return p


class Handler(BaseHTTPRequestHandler):
    server_version = 'MediaLib'


    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body=b'', ctype='application/json; charset=utf-8', extra=None):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), 'application/json; charset=utf-8')

    def do_GET(self):
        if not hasattr(self, '_json_body'):
            self._json_body = {}
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path

        if p == '/' or p == '/index.html':
            try:
                with open(app_html_file(), 'r', encoding='utf-8') as f:
                    self._send(200, f.read(), 'text/html; charset=utf-8')
            except FileNotFoundError:
                self._send(500, 'app.html 缺失', 'text/plain; charset=utf-8')
            return

        if p == '/loading':
            self._send(200, LOADING_HTML, 'text/html; charset=utf-8')
            return

        if p == '/api/progress':
            self._json(get_progress())
            return

        if p == '/api/facets':
            self.api_facets(q)
            return

        if p == '/api/items':
            self.api_items(q)
            return

        if p == '/api/albums':
            self.api_albums()
            return

        m = re.match(r'^/thumb/(\d+)$', p)
        if m:
            self.serve_thumb(int(m.group(1)))
            return

        m = re.match(r'^/api/item/(\d+)$', p)
        if m:
            self.api_item(int(m.group(1)))
            return

        m = re.match(r'^/preview/(\d+)$', p)
        if m:
            self.serve_preview(int(m.group(1)))
            return

        m = re.match(r'^/file/(\d+)$', p)
        if m:
            self.serve_file(int(m.group(1)))
            return

        m = re.match(r'^/play/(\d+)$', p)
        if m:
            self.serve_play(int(m.group(1)))
            return

        m = re.match(r'^/api/mpvplay/(\d+)$', p)
        if m:
            self.api_mpvplay(int(m.group(1)))
            return

        m = re.match(r'^/api/playprep/(\d+)$', p)
        if m:
            self.api_playprep(int(m.group(1)), q)
            return

        m = re.match(r'^/api/open/(\d+)$', p)
        if m:
            self.api_open(int(m.group(1)), 'open')
            return

        m = re.match(r'^/api/reveal/(\d+)$', p)
        if m:
            self.api_open(int(m.group(1)), 'reveal')
            return

        if p == '/api/batchdelete':
            api_batch_delete(self, (q.get('ids') or [''])[0])
            return

        m = re.match(r'^/api/delete/(\d+)$', p)
        if m:
            api_delete(self, int(m.group(1)))
            return

        m = re.match(r'^/api/rename/(\d+)$', p)
        if m:
            api_rename(self, int(m.group(1)), (q.get('name') or [''])[0])
            return

        m = re.match(r'^/api/copyclip/(\d+)$', p)
        if m:
            api_clip(self, int(m.group(1)), False)
            return

        m = re.match(r'^/api/cutclip/(\d+)$', p)
        if m:
            api_clip(self, int(m.group(1)), True)
            return

        if p == '/api/trash':
            api_trash_list(self); return
        if p == '/api/trash/add':
            api_trash_add(self, (q.get('ids') or [''])[0]); return
        if p == '/api/trash/restore':
            api_trash_restore(self, (q.get('ids') or [''])[0]); return
        if p == '/api/trash/undo':
            api_trash_undo(self); return
        if p == '/api/trash/clear':
            api_trash_clear(self); return

        if p == '/api/tags':
            api_tags(self); return
        if p == '/api/tag/add':
            api_tag_add(self, (q.get('ids') or [''])[0], (q.get('name') or [''])[0]); return
        if p == '/api/tag/remove':
            api_tag_remove(self, (q.get('ids') or [''])[0], (q.get('name') or [''])[0]); return
        if p == '/api/tag/delete':
            api_tag_delete(self, (q.get('name') or [''])[0]); return
        if p == '/api/fav':
            api_fav(self, (q.get('ids') or [''])[0], (q.get('on') or ['0'])[0] == '1'); return
        m = re.match(r'^/api/itemtags/(\d+)$', p)
        if m:
            api_item_tags(self, int(m.group(1))); return

        if p == '/api/dedupe/start':
            ok = start_dedupe()
            self._json({'ok': ok, 'msg': '已开始分析' if ok else '正在分析中'}); return
        if p == '/api/dedupe/status':
            g = dup_groups() if not DEDUPE_JOB.get('running') and DEDUPE_JOB.get('phase') in ('ready', 'idle') else None
            self._json({'job': DEDUPE_JOB, 'exact_count': (g or {}).get('exact_count', 0),
                        'similar_count': (g or {}).get('similar_count', 0)}); return
        if p == '/api/dedupe/plan':
            self._json(dup_exact_plan()); return
        if p == '/api/dedupe/groups':
            g = dup_groups()
            g['job'] = DEDUPE_JOB
            self._json(g); return

        if p == '/api/content/start':
            ok = start_content((q.get('scope') or ['smart'])[0])
            self._json({'ok': ok, 'job': CONTENT_JOB, 'ocr': ocr_available()}); return
        if p == '/api/content/stop':
            CONTENT_JOB['stop'] = True
            self._json({'ok': True}); return
        if p == '/api/content/status':
            con = connect()
            indexed = con.execute('SELECT COUNT(*) FROM items WHERE content<>""').fetchone()[0]
            pending = con.execute('SELECT COUNT(*) FROM items WHERE media="文档" AND (content_state=0 OR content_mtime<mtime)').fetchone()[0]
            con.close()
            self._json({'job': CONTENT_JOB, 'indexed': indexed, 'pending_docs': pending, 'ocr': ocr_available()}); return

        if p == '/api/batchrename':
            api_batch_rename(self, (q.get('ids') or [''])[0], (q.get('pattern') or [''])[0],
                             (q.get('start') or ['1'])[0]); return
        if p == '/api/batchmove':
            api_batch_move(self, (q.get('ids') or [''])[0], (q.get('dest') or [''])[0],
                           (q.get('mkdir') or ['0'])[0] == '1'); return
        if p == '/api/batchconvert':
            api_batch_convert(self, (q.get('ids') or [''])[0], (q.get('fmt') or ['webp'])[0],
                              (q.get('quality') or ['85'])[0], (q.get('dest') or [''])[0]); return

        if p == '/api/convert/start':
            self.api_convert_start(); return
        if p == '/api/convert/status':
            self._json({'ok': True, 'job': convert_status()}); return

        m = re.match(r'^/api/wm/preview/(\d+)$', p)
        if m:
            self.api_wm_preview(int(m.group(1))); return
        if p == '/api/watermark/remove':
            self.api_watermark_remove(); return
        if p == '/api/watermark/auto':
            self.api_watermark_auto(); return

        if p == '/api/discover/sources':
            self._json(discover_sources()); return
        if p == '/api/discover/search':
            self.api_discover_search(q); return
        if p == '/api/discover/site':
            self.api_discover_site(); return
        if p == '/api/discover/thumb':
            self.api_discover_thumb(q); return
        if p == '/api/discover/download':
            self.api_discover_download(); return
        if p == '/api/discover/status':
            self._json({'ok': True, 'job': discover_status()}); return
        if p == '/api/discover/ingest':
            self.api_discover_ingest(); return

        self._send(404, 'not found', 'text/plain; charset=utf-8')

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        # 打开/定位这类动作走 POST，复用 GET 的分发；转换/去水印需要读 JSON 请求体
        self._json_body = {}
        try:
            n = int(self.headers.get('Content-Length') or 0)
            if 0 < n <= 48 * 1024 * 1024:
                raw = self.rfile.read(n)
                if raw:
                    self._json_body = json.loads(raw.decode('utf-8'))
        except Exception:
            self._json_body = {}
        self.do_GET()

    def api_convert_start(self):
        d = self._json_body or {}
        ok, msg = start_convert(d.get('ids') or [], d.get('target') or 'pdf',
                                d.get('dpi') or 200, d.get('dest') or '')
        self._json({'ok': ok, 'msg': msg, 'job': convert_status()})

    def api_wm_preview(self, item_id):
        con = connect()
        row = con.execute('SELECT path, ext, media FROM items WHERE id=?', (item_id,)).fetchone()
        con.close()
        if not row or row[2] != '图片' or not os.path.exists(row[0]):
            self._send(404, '这张图片已不在原位', 'text/plain; charset=utf-8')
            return
        try:
            from PIL import Image, ImageOps
            with Image.open(row[0]) as im:
                im = ImageOps.exif_transpose(im)
                im.thumbnail((2400, 2400), Image.LANCZOS)
                if im.mode not in ('RGB', 'RGBA'):
                    im = im.convert('RGBA' if 'A' in im.getbands() else 'RGB')
                buf = io.BytesIO()
                im.save(buf, 'PNG', optimize=True)
            self._send(200, buf.getvalue(), 'image/png')
        except Exception as e:
            self._send(404, '这个图片格式解不出来，暂时不能去水印：' + str(e)[:80],
                       'text/plain; charset=utf-8')

    def api_watermark_remove(self):
        d = self._json_body or {}
        try:
            item_id = int(d.get('id') or 0)
        except (TypeError, ValueError):
            item_id = 0
        mask_s = d.get('mask') or ''
        if not item_id or not mask_s:
            self._json({'ok': False, 'msg': '缺少图片或涂抹遮罩'}, 400)
            return
        con = connect()
        row = con.execute('SELECT path, name, ext, media FROM items WHERE id=?', (item_id,)).fetchone()
        con.close()
        if not row or row[3] != '图片' or not os.path.exists(row[0]):
            self._json({'ok': False, 'msg': '这张图片已不在原位'}, 404)
            return
        path, name, ext, media = row
        try:
            target = watermark_remove_file(path, name, mask_s, d.get('radius') or 4)
            _index_output(target, '图片')
            bump_data()
            self._json({'ok': True, 'path': target, 'name': os.path.basename(target),
                        'id': self._lookup_id(target), 'msg': '已生成去水印副本'})
        except Exception as e:
            self._json({'ok': False, 'msg': '去水印失败：' + str(e)[:180]}, 500)

    def api_watermark_auto(self):
        d = self._json_body or {}
        try:
            item_id = int(d.get('id') or 0)
        except (TypeError, ValueError):
            item_id = 0
        if not item_id:
            self._json({'ok': False, 'msg': '缺少图片'}, 400)
            return
        con = connect()
        row = con.execute('SELECT path, name, ext, media FROM items WHERE id=?', (item_id,)).fetchone()
        con.close()
        if not row or row[3] != '图片' or not os.path.exists(row[0]):
            self._json({'ok': False, 'msg': '这张图片已不在原位'}, 404)
            return
        try:
            target, pixels, boxes = watermark_auto_file(row[0], row[1],
                                                        d.get('strength') or 3, d.get('radius') or 5)
            _index_output(target, '图片')
            bump_data()
            self._json({'ok': True, 'path': target, 'name': os.path.basename(target),
                        'id': self._lookup_id(target), 'pixels': pixels, 'regions': boxes,
                        'msg': '已自动识别 %d 处水印区域并生成副本' % max(1, boxes)})
        except Exception as e:
            self._json({'ok': False, 'msg': '一键去水印失败：' + str(e)[:180]}, 500)

    def api_discover_search(self, q):
        try:
            r = discover_search((q.get('q') or [''])[0], (q.get('kinds') or [''])[0],
                                (q.get('sources') or [''])[0], (q.get('limit') or ['16'])[0])
            self._json(r)
        except Exception as e:
            self._json({'ok': False, 'msg': '搜索失败：' + str(e)[:180], 'items': [], 'status': []}, 500)

    def api_discover_site(self):
        d = self._json_body or {}
        try:
            r = discover_site(d.get('url') or '', d.get('q') or '', d.get('kinds') or '',
                              d.get('max_pages') or 5, d.get('max_items') or 80)
            self._json(r)
        except Exception as e:
            self._json({'ok': False, 'msg': '嗅探失败：' + str(e)[:180], 'items': []}, 500)

    def api_discover_download(self):
        d = self._json_body or {}
        if not (d.get('url') or '').strip():
            self._json({'ok': False, 'msg': '缺少资源地址'}, 400)
            return
        meta = {'url': d.get('url'), 'name': d.get('name') or '', 'kind': d.get('kind') or '图片',
                'source': d.get('source') or '', 'license': d.get('license') or '',
                'page': d.get('page') or '', 'title': d.get('title') or ''}
        ok, msg = start_discover_download(meta)
        self._json({'ok': ok, 'msg': msg, 'token': msg if ok else ''}, 200 if ok else 409)

    def api_discover_ingest(self):
        d = self._json_body or {}
        r = discover_ingest(d.get('token') or '')
        self._json(r, 200 if r.get('ok') else 400)

    def api_discover_thumb(self, q):
        url = (q.get('url') or [''])[0]
        try:
            p = discover_thumb_path(url)
            with open(p, 'rb') as f:
                data = f.read()
            self._send(200, data, 'image/png', {'X-Content-Type-Options': 'nosniff'})
        except Exception as e:
            self._send(404, str(e)[:180], 'text/plain; charset=utf-8')

    def _lookup_id(self, path):
        try:
            con = connect()
            r = con.execute('SELECT id FROM items WHERE path=?', (path,)).fetchone()
            con.close()
            return r[0] if r else 0
        except Exception:
            return 0

    def api_facets(self, q=None):
        def gv(k, d=''):
            return ((q or {}).get(k) or [d])[0]
        t = gv('type', '')
        key = ('facets', t, _DATA_VER)
        cached = _STATS_CACHE.get(key)
        if cached is not None:
            self._json(cached)
            return
        con = connect()
        total = con.execute('SELECT COUNT(*) FROM items').fetchone()[0]
        media = dict(con.execute('SELECT media, COUNT(*) FROM items GROUP BY media').fetchall())
        cats = con.execute('SELECT cat, COUNT(*) c FROM items GROUP BY cat ORDER BY c DESC').fetchall()
        locs = con.execute('SELECT loc, COUNT(*) c FROM items GROUP BY loc ORDER BY c DESC LIMIT 60').fetchall()
        subs = con.execute("SELECT cat, sub, COUNT(*) c FROM items WHERE sub<>'' "
                           "GROUP BY cat, sub ORDER BY c DESC LIMIT 80").fetchall()
        favs = con.execute('SELECT COUNT(*) FROM fav f JOIN items i ON i.id=f.item_id').fetchone()[0]
        tags = con.execute('SELECT t.name,COUNT(it.item_id) FROM tags t LEFT JOIN item_tags it ON it.tag_id=t.id'
                           ' JOIN items i ON i.id=it.item_id GROUP BY t.id ORDER BY COUNT(it.item_id) DESC,t.name'
                           ' LIMIT 80').fetchall()
        if t and t != '全部':
            disks = con.execute("SELECT substr(path,1,2) d, COUNT(*) c FROM items "
                                "WHERE length(path)>3 AND media=? GROUP BY d ORDER BY d", (t,)).fetchall()
        else:
            disks = con.execute("SELECT substr(path,1,2) d, COUNT(*) c FROM items "
                                "WHERE length(path)>3 GROUP BY d ORDER BY d").fetchall()
        con.close()
        data = {'total': total, 'media': media,
                'cats': [{'k': k, 'n': n} for k, n in cats],
                'locs': [{'k': k, 'n': n} for k, n in locs],
                'disks': [{'k': k, 'n': n} for k, n in disks if k and k[0].isalpha() and k[1] == ':'],
                'subs': [{'cat': c, 'k': s, 'n': n} for c, s, n in subs],
                'tags': [{'name': k, 'n': n} for k, n in tags], 'fav': favs,
                'trash': _trash_count(), 'meta': META_JOB}
        _STATS_CACHE[key] = data
        self._json(data)

    def api_items(self, q):
        def gv(k, d=''):
            return (q.get(k) or [d])[0]

        p = {'type': gv('type', '全部'), 'cat': gv('cat', '全部'), 'loc': gv('loc', '全部'),
             'sub': gv('sub', ''), 'q': gv('q', '').strip(), 'ext': gv('ext', ''), 'recent': gv('recent', ''),
             'disk': gv('disk', ''), 'tag': gv('tag', ''), 'fav': gv('fav', ''),
             'orient': gv('orient', ''), 'minw': gv('minw', ''), 'minh': gv('minh', ''),
             'alpha': gv('alpha', ''), 'durmin': gv('durmin', ''), 'durmax': gv('durmax', '')}
        sort = gv('sort', 'time')
        try:
            offset = max(0, int(gv('offset', '0')))
            limit = min(200, max(1, int(gv('limit', '120'))))
        except ValueError:
            offset, limit = 0, 120

        wsql, args = _filter(p)
        orders = {'name': 'name COLLATE NOCASE', 'time': 'mtime DESC', 'size': 'size DESC', 'path': 'path COLLATE NOCASE',
                  'pixels': '(COALESCE(w,0)*COALESCE(h,0)) DESC'}
        order = orders.get(sort, orders['time'])

        con = connect()
        total = con.execute('SELECT COUNT(*) FROM items ' + wsql, args).fetchone()[0]
        rows = con.execute(
            'SELECT id,name,ext,media,size,mtime,loc,cat,w,h,path,sub,'
            '(SELECT 1 FROM fav f WHERE f.item_id=items.id) fav,'
            '(SELECT group_concat(t.name,\'|\') FROM item_tags it JOIN tags t ON t.id=it.tag_id WHERE it.item_id=items.id) tags'
            ' FROM items ' + wsql +
            ' ORDER BY ' + order + ' LIMIT ? OFFSET ?', args + [limit, offset]).fetchall()
        con.close()
        items = [{'id': r[0], 'name': r[1], 'ext': r[2], 'media': r[3], 'size': r[4],
                  'mtime': r[5], 'loc': r[6], 'cat': r[7], 'w': r[8], 'h': r[9], 'path': r[10],
                  'sub': r[11] or '', 'fav': bool(r[12]), 'tags': (r[13] or '').split('|') if r[13] else []}
                 for r in rows]
        self._json({'total': total, 'items': items, 'offset': offset})

    def api_item(self, item_id):
        con = connect()
        r = con.execute('SELECT id,name,ext,media,size,mtime,loc,cat,w,h,path,sub,'
                        '(SELECT 1 FROM fav f WHERE f.item_id=items.id),'
                        '(SELECT group_concat(t.name,\'|\') FROM item_tags it JOIN tags t ON t.id=it.tag_id WHERE it.item_id=items.id)'
                        ' FROM items WHERE id=?', (item_id,)).fetchone()
        con.close()
        if not r:
            self._json({'error': 'not found'}, 404)
            return
        self._json({'id': r[0], 'name': r[1], 'ext': r[2], 'media': r[3], 'size': r[4], 'mtime': r[5],
                    'loc': r[6], 'cat': r[7], 'w': r[8], 'h': r[9], 'path': r[10], 'sub': r[11] or '',
                    'fav': bool(r[12]), 'tags': (r[13] or '').split('|') if r[13] else []})

    def api_albums(self):
        key = ('albums', _DATA_VER)
        cached = _STATS_CACHE.get(key)
        if cached is not None:
            self._json(cached)
            return
        con = connect()
        cats = con.execute('SELECT cat, COUNT(*) c FROM items GROUP BY cat ORDER BY c DESC').fetchall()
        specs = [
            {'key': 'fav', 'name': '★ 我的收藏', 'type': '全部', 'cat': '全部', 'fav': '1'},
            {'key': 'recent7', 'name': '最近 7 天', 'type': '全部', 'cat': '全部', 'recent': '7'},
            {'key': 'recent30', 'name': '最近 30 天', 'type': '全部', 'cat': '全部', 'recent': '30'},
            {'key': 'all', 'name': '全部素材', 'type': '全部', 'cat': '全部'},
            {'key': 'img', 'name': '图片', 'type': '图片', 'cat': '全部'},
            {'key': 'video', 'name': '视频', 'type': '视频', 'cat': '全部'},
            {'key': 'audio', 'name': '音频', 'type': '音频', 'cat': '全部'},
            {'key': 'gif', 'name': '动图 GIF', 'type': '图片', 'cat': '全部', 'ext': '.gif'},
        ]
        for c, n in cats:
            if c in ('图片', '视频', '音频'):
                continue
            specs.append({'key': 'cat:' + c, 'name': c, 'type': '全部', 'cat': c})
        # 细分相册：项目里的部件（特效/界面/茶叶…）单独成册
        subs = con.execute("SELECT cat, sub, COUNT(*) c FROM items WHERE sub<>'' "
                           "GROUP BY cat, sub HAVING c >= 12 ORDER BY c DESC LIMIT 12").fetchall()
        for c, s, n in subs:
            specs.append({'key': 'sub:' + c + '|' + s, 'name': s, 'type': '全部', 'cat': c, 'sub': s})
        tags = con.execute('SELECT t.name,COUNT(it.item_id) c FROM tags t JOIN item_tags it ON it.tag_id=t.id'
                           ' JOIN items i ON i.id=it.item_id GROUP BY t.id HAVING c>0 ORDER BY c DESC LIMIT 16').fetchall()
        for name, n in tags:
            specs.append({'key': 'tag:' + name, 'name': '# ' + name, 'type': '全部', 'cat': '全部', 'tag': name})
        out = []
        for sp in specs:
            wsql, args = _filter(sp)
            n = con.execute('SELECT COUNT(*) FROM items ' + wsql, args).fetchone()[0]
            if n == 0:
                continue
            covers = [r[0] for r in con.execute(
                'SELECT id FROM items ' + wsql + ' ORDER BY mtime DESC LIMIT 4', args).fetchall()]
            out.append({'key': sp['key'], 'name': sp['name'], 'n': n, 'covers': covers, 'spec': sp})
        con.close()
        data = {'albums': out}
        _STATS_CACHE[key] = data
        self._json(data)

    def serve_thumb(self, item_id):
        con = connect()
        row = con.execute('SELECT path, mtime, media, ext FROM items WHERE id=?', (item_id,)).fetchone()
        con.close()
        if not row:
            self._send(404, '', 'image/webp')
            return
        path, mtime, media, ext = row
        out = thumb_for(item_id, path, mtime, media, ext)
        if not out:
            self._send(404, '', 'image/webp')
            return
        try:
            with open(out, 'rb') as f:
                data = f.read()
        except OSError:
            self._send(404, '', 'image/webp')
            return
        self._send(200, data, 'image/webp', {'Cache-Control': 'max-age=86400'})

    def serve_preview(self, item_id):
        con = connect()
        row = con.execute('SELECT path, ext, mtime FROM items WHERE id=?', (item_id,)).fetchone()
        con.close()
        if not row:
            self._send(404, '没有这个文件', 'text/plain; charset=utf-8')
            return
        path, ext, mtime = row
        ext = (ext or '').lower()
        if ext in BROWSER_IMG:
            self.serve_file(item_id)
            return
        out = preview_for(item_id, path, mtime)
        if not out:
            self._send(404, '这个格式解不出来（可能损坏或需要专用插件）：' + ext, 'text/plain; charset=utf-8')
            return
        try:
            with open(out, 'rb') as f:
                data = f.read()
        except OSError:
            self._send(404, '预览文件读取失败', 'text/plain; charset=utf-8')
            return
        self._send(200, data, 'image/png', {'Cache-Control': 'max-age=86400'})

    def serve_file(self, item_id):
        con = connect()
        row = con.execute('SELECT path, ext FROM items WHERE id=?', (item_id,)).fetchone()
        con.close()
        if not row:
            self._send(404, '', 'text/plain')
            return
        path, ext = row
        if not os.path.exists(path):
            self._send(404, '文件已不在原位', 'text/plain; charset=utf-8')
            return
        ctype = {
            '.mp4': 'video/mp4', '.webm': 'video/webm', '.mov': 'video/quicktime', '.mkv': 'video/x-matroska',
            '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.m4a': 'audio/mp4', '.ogg': 'audio/ogg',
            '.flac': 'audio/flac', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
            '.webp': 'image/webp', '.gif': 'image/gif', '.svg': 'image/svg+xml', '.bmp': 'image/bmp',
            '.ico': 'image/x-icon', '.avif': 'image/avif', '.tif': 'image/tiff', '.tiff': 'image/tiff',
        }.get(ext, 'application/octet-stream')

        size = os.path.getsize(path)
        rng = self.headers.get('Range')
        start, end = 0, size - 1
        if rng and rng.startswith('bytes='):
            m = re.match(r'bytes=(\d*)-(\d*)', rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
                if start > end:
                    start, end = 0, size - 1
        length = end - start + 1
        code = 206 if rng else 200
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(length))
        if rng:
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
        self.end_headers()
        if self.command == 'HEAD':
            return
        try:
            with open(path, 'rb') as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _send_path(self, path, ctype):
        """按 Range 把一个本地文件发给浏览器"""
        size = os.path.getsize(path)
        rng = self.headers.get('Range')
        start, end = 0, size - 1
        if rng and rng.startswith('bytes='):
            m = re.match(r'bytes=(\d*)-(\d*)', rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
                if start > end:
                    start, end = 0, size - 1
        length = end - start + 1
        code = 206 if rng else 200
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(length))
        if rng:
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
        self.end_headers()
        if self.command == 'HEAD':
            return
        try:
            with open(path, 'rb') as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def serve_play(self, item_id):
        con = connect()
        row = con.execute('SELECT mtime, ext FROM items WHERE id=?', (item_id,)).fetchone()
        con.close()
        if not row:
            self._send(404, '', 'text/plain')
            return
        mtime, ext = row
        out = play_cache_path(item_id, mtime)
        if not os.path.exists(out):
            self._send(404, '还没转好，先刷新一下', 'text/plain; charset=utf-8')
            return
        self._send_path(out, 'video/mp4')

    def api_mpvplay(self, item_id):
        con = connect()
        row = con.execute('SELECT path, name FROM items WHERE id=?', (item_id,)).fetchone()
        con.close()
        if not row:
            self._json({'ok': False, 'msg': '没有这条记录'}, 404)
            return
        path, name = row
        if not os.path.exists(path):
            self._json({'ok': False, 'msg': '文件已不在原位'})
            return
        ok, msg = launch_mpv(path, name)
        self._json({'ok': ok, 'msg': msg})

    def api_playprep(self, item_id, q=None):
        allow = ((q or {}).get('transcode') or ['0'])[0] in ('1', 'true', 'yes')
        con = connect()
        row = con.execute('SELECT path, ext, mtime FROM items WHERE id=?', (item_id,)).fetchone()
        con.close()
        if not row:
            self._json({'ok': False, 'mode': 'error', 'msg': '没有这条记录'}, 404)
            return
        path, ext, mtime = row
        if not os.path.exists(path):
            self._json({'ok': False, 'mode': 'error', 'msg': '文件已不在原位'})
            return
        try:
            st = play_state(item_id, path, mtime, ext, allow_transcode=allow)
            st['ok'] = st.get('mode') != 'error'
            self._json(st)
        except Exception as e:
            self._json({'ok': False, 'mode': 'error', 'msg': '准备播放失败：' + str(e)[:120]})

    def api_open(self, item_id, mode):
        con = connect()
        row = con.execute('SELECT path, w, h FROM items WHERE id=?', (item_id,)).fetchone()
        if not row:
            con.close()
            self._json({'ok': False, 'msg': '没有这条记录'}, 404)
            return
        path, w, h = row
        if not os.path.exists(path):
            con.close()
            self._json({'ok': False, 'msg': '文件已不在原位'}, 404)
            return
        try:
            if mode == 'reveal':
                subprocess.Popen('explorer /select,"%s"' % path)
            else:
                os.startfile(path)
        except Exception as e:
            con.close()
            self._json({'ok': False, 'msg': str(e)}, 500)
            return
        # 顺手补上图片尺寸
        if w is None and media_of(os.path.splitext(path)[1]):
            ww, hh = image_size(path)
            if ww:
                con.execute('UPDATE items SET w=?, h=? WHERE id=?', (ww, hh, item_id))
                con.commit()
        con.close()
        self._json({'ok': True})


def _row_path(item_id):
    con = connect()
    row = con.execute('SELECT path, name, ext, size, mtime FROM items WHERE id=?', (item_id,)).fetchone()
    con.close()
    return row


def set_explorer_clipboard(path, cut=False):
    """把文件放进 Windows 剪贴板（CF_HDROP），资源管理器里可直接粘贴；cut=True 为剪切"""
    import ctypes, struct
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    u32 = ctypes.windll.user32
    u32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    u32.SetClipboardData.restype = ctypes.c_void_p
    u32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    u32.RegisterClipboardFormatW.restype = wintypes.UINT
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalAlloc.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    k32.GlobalFree.argtypes = [ctypes.c_void_p]

    def _put(fmt, data):
        h = k32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        if not h:
            raise OSError('GlobalAlloc 失败')
        p = k32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        k32.GlobalUnlock(h)
        if not u32.SetClipboardData(fmt, h):
            k32.GlobalFree(h)
            raise OSError('SetClipboardData 失败')

    # DROPFILES 头：pFiles=20, pt=(0,0), fNC=0, fWide=1，后面是双 null 结尾的 UTF-16 路径
    files = path.encode('utf-16-le') + b'\x00\x00' + b'\x00\x00'
    blob = struct.pack('<IiiII', 20, 0, 0, 0, 1) + files
    if not u32.OpenClipboard(None):
        raise OSError('剪贴板被别的程序占用')
    try:
        u32.EmptyClipboard()
        _put(15, blob)  # CF_HDROP
        fmt = u32.RegisterClipboardFormatW('Preferred DropEffect')
        if fmt:
            _put(fmt, struct.pack('<I', 2 if cut else 1))
    finally:
        u32.CloseClipboard()


def recycle_file(path):
    """用 Windows 回收站删除（Microsoft.VisualBasic），不弹确认框、不留黑窗"""
    import base64
    script = ("Add-Type -AssemblyName Microsoft.VisualBasic;"
              "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($env:ML_TARGET,"
              "'OnlyErrorDialogs','SendToRecycleBin')")
    enc = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    env = dict(os.environ)
    env['ML_TARGET'] = path
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    r = subprocess.run([os.path.join(os.environ.get('SystemRoot', r'C:\Windows'),
                                     'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe'),
                        '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden',
                        '-EncodedCommand', enc],
                       env=env, capture_output=True, creationflags=flags, timeout=30)
    if os.path.exists(path):
        msg = (r.stderr or b'').decode('utf-8', 'ignore').strip() or '删除失败（可能没有回收站权限）'
        return False, msg
    return True, '已移到回收站'


def api_delete(self, item_id):
    api_trash_add(self, str(item_id))


def api_batch_delete(self, ids_s):
    api_trash_add(self, ids_s)


def api_rename(self, item_id, new_name):
    row = _row_path(item_id)
    if not row:
        self._json({'ok': False, 'msg': '没有这条记录'}, 404)
        return
    path, name, ext, size, mtime = row
    if not os.path.exists(path):
        self._json({'ok': False, 'msg': '文件已不在原位，无法重命名'}, 404)
        return
    new_name = (new_name or '').strip()
    if not new_name:
        self._json({'ok': False, 'msg': '文件名不能为空'}, 400)
        return
    if any(ch in new_name for ch in '\\/:*?"<>|') or new_name in ('.', '..'):
        self._json({'ok': False, 'msg': '文件名不能包含 \\ / : * ? " < > |'}, 400)
        return
    if os.path.splitext(new_name)[1] == '' and ext:
        new_name += ext
    folder = os.path.dirname(path)
    target = os.path.join(folder, new_name)
    if os.path.normcase(target) == os.path.normcase(path):
        self._json({'ok': True, 'msg': '文件名没有变化'})
        return
    if os.path.exists(target):
        self._json({'ok': False, 'msg': '同目录下已经有同名文件'}, 400)
        return
    try:
        os.rename(path, target)
    except OSError as e:
        self._json({'ok': False, 'msg': '重命名失败：' + e.strerror}, 500)
        return
    st = os.stat(target)
    n_ext = os.path.splitext(new_name)[1].lower()
    old_media = media_of(ext)
    new_media = media_of(n_ext)
    con = connect()
    if new_media != old_media:
        con.execute('UPDATE items SET path=?, name=?, ext=?, media=?, size=?, mtime=?, cat=?, sub=?, '
                    'w=NULL, h=NULL, alpha=0, dur=0, phash=\'\', hash_mtime=0, meta_state=0 WHERE id=?',
                    (target, new_name, n_ext, new_media, st.st_size, int(st.st_mtime),
                     guess_cat(target, new_media), sub_of(target, new_media), item_id))
    else:
        con.execute('UPDATE items SET path=?, name=?, ext=?, size=?, mtime=?, cat=?, sub=? WHERE id=?',
                    (target, new_name, n_ext, st.st_size, int(st.st_mtime),
                     guess_cat(target, new_media), sub_of(target, new_media), item_id))
    con.commit(); con.close()
    bump_data()
    if new_media in ('图片', '视频'):
        start_meta_index()
    self._json({'ok': True, 'msg': '已重命名为 ' + new_name, 'name': new_name, 'path': target})


def api_clip(self, item_id, cut):
    row = _row_path(item_id)
    if not row:
        self._json({'ok': False, 'msg': '没有这条记录'}, 404)
        return
    path = row[0]
    if not os.path.exists(path):
        self._json({'ok': False, 'msg': '文件已不在原位'}, 404)
        return
    try:
        set_explorer_clipboard(path, cut)
    except Exception as e:
        self._json({'ok': False, 'msg': '复制到剪贴板失败：' + str(e)}, 500)
        return
    self._json({'ok': True, 'msg': '已剪切，可到资源管理器粘贴' if cut else '已复制，可到资源管理器粘贴'})


def find_running():
    """看看是不是已经有素材库在跑，有就返回它的地址（优先读运行端口，避免逐端口探测拖慢启动）"""
    ports = []
    try:
        with open(os.path.join(IDX, 'runtime_port.txt'), 'r', encoding='ascii') as f:
            p = int(f.read().strip())
        if 8765 <= p <= 8785:
            ports.append(p)
    except Exception:
        pass
    ports.append(8765)
    for port in dict.fromkeys(ports):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.08)
        try:
            if s.connect_ex(('127.0.0.1', port)) != 0:
                continue
        finally:
            s.close()
        url = 'http://127.0.0.1:%d/' % port
        try:
            with urllib.request.urlopen(url + 'api/facets', timeout=0.25) as r:
                data = json.loads(r.read().decode('utf-8'))
            if 'total' in data and 'cats' in data:
                return url
        except Exception:
            continue
    return None


def db_count():
    if not os.path.exists(DB):
        return 0
    try:
        con = sqlite3.connect(DB, timeout=10)
        n = con.execute('SELECT COUNT(*) FROM items').fetchone()[0]
        con.close()
        return n
    except Exception:
        return 0


def _low_priority():
    """后台扫描/查重降到低优先级，前台用电脑不被拖慢"""
    if os.name != 'nt':
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadPriority(ctypes.windll.kernel32.GetCurrentThread(), -1)
    except Exception:
        pass


def scan_in_thread():
    """后台扫描，进度写进 PROGRESS，界面上的进度条靠它刷新"""
    _low_priority()
    try:
        do_scan()
    except Exception as e:
        set_progress(phase='error', msg=str(e))
        print('扫描失败：%s' % e)


def start_http(quiet=False, defer_scan=False):
    """起本地服务（后台线程跑），返回 (url, httpd, scanning)。已经有实例就直接返回它的地址"""
    running = find_running()
    if running:
        return running, None, False
    need = db_count() == 0
    if need and not defer_scan:
        if not quiet:
            print('还没有索引，先扫描一遍…')
        do_scan()
        need = False
    if not os.path.exists(app_html_file()):
        raise RuntimeError('缺少 _工具\\app.html')
    port = 8765
    for _ in range(20):
        try:
            httpd = ThreadingHTTPServer(('127.0.0.1', port), Handler)
            break
        except OSError:
            port += 1
    else:
        raise RuntimeError('找不到可用端口')
    url = 'http://127.0.0.1:%d/' % port
    try:
        with open(os.path.join(IDX, 'runtime_port.txt'), 'w', encoding='ascii') as f:
            f.write(str(port))
    except Exception:
        pass
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    threading.Thread(target=auto_scan_loop, daemon=True).start()
    start_meta_index()
    if need:
        set_progress(phase='scan', scanned=0, roots=0, ri=0, root='',
                     t0=time.time(), total_db=0, msg='正在准备扫描…')
        threading.Thread(target=scan_in_thread, daemon=True).start()
    return url, httpd, need


def serve(no_open=False):
    if os.name == 'nt':
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW('素材坞 · AssetDock')
        except Exception:
            pass
    url, httpd, _scan = start_http()
    if httpd is None:
        print('素材坞已经在运行：' + url)
        if not no_open:
            webbrowser.open(url)
        return
    print('素材坞已启动：' + url)
    print('关掉窗口即停止服务。开着的时候每 15 分钟自动补扫，新素材会自动出现。')
    if not no_open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print('已停止')


LOADING_HTML = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>素材坞</title>
<style>
  :root{--acc:#8b6cff;--acc2:#4bd1c4;--txt:#e8ecf4;--dim:#8b93a7;--dim2:#5d6478;--line:rgba(49,58,76,.7)}
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;background:#0f1116;color:var(--txt);
    font-family:'Microsoft YaHei UI',system-ui,-apple-system,sans-serif;
    display:flex;align-items:center;justify-content:center;overflow:hidden}
  body::before{content:'';position:fixed;inset:-20%;pointer-events:none;
    background:radial-gradient(520px 420px at 22% 18%,rgba(139,108,255,.16),transparent 70%),
                radial-gradient(560px 460px at 78% 82%,rgba(75,209,196,.12),transparent 72%)}
  .wrap{position:relative;width:min(560px,86vw);text-align:center}
  .logo{width:54px;height:54px;margin:0 auto 18px;display:grid;grid-template-columns:1fr 1fr;
    grid-template-rows:1fr 1fr;gap:3px;padding:4px;border-radius:14px;
    background:#0b0d12;border:1px solid rgba(255,255,255,.06);box-shadow:0 10px 30px rgba(0,0,0,.5)}
  .logo i{border-radius:5px;display:block}
  .logo i:nth-child(1){background:linear-gradient(135deg,#a78bfa,#7c5cff)}
  .logo i:nth-child(2){background:linear-gradient(135deg,#5eead4,#2fb6a8)}
  .logo i:nth-child(3){background:linear-gradient(135deg,#fbbf24,#f59e0b)}
  .logo i:nth-child(4){background:linear-gradient(135deg,#f472b6,#e11d63)}
  h1{font-size:23px;letter-spacing:2px;margin:0 0 8px;font-weight:600}
  .sub{font-size:13px;color:var(--dim);margin-bottom:26px}
  .bar{position:relative;height:8px;border-radius:99px;background:rgba(255,255,255,.055);
    overflow:hidden;border:1px solid rgba(255,255,255,.06)}
  .bar::after{content:'';position:absolute;top:0;bottom:0;width:42%;border-radius:99px;
    background:linear-gradient(90deg,var(--acc),var(--acc2));
    animation:slide 1.35s cubic-bezier(.45,0,.55,1) infinite;box-shadow:0 0 16px rgba(139,108,255,.45)}
  .bar.done{background:linear-gradient(90deg,rgba(139,108,255,.5),rgba(75,209,196,.5))}
  .bar.done::after{width:100%;animation:none}
  @keyframes slide{0%{left:-45%}100%{left:100%}}
  .nums{display:flex;justify-content:center;gap:26px;margin:22px 0 6px;flex-wrap:wrap}
  .k{font-size:11px;letter-spacing:1.2px;color:var(--dim2);text-transform:uppercase}
  .v{font-size:19px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums}
  .v em{font-style:normal;font-size:11.5px;color:var(--dim);margin-left:3px;font-weight:400}
  .path{font-size:11.5px;color:var(--dim2);margin-top:16px;height:18px;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;direction:rtl;text-align:center}
  .tip{font-size:12px;color:#4f5669;margin-top:26px;line-height:1.9}
  .err{color:#f87171;font-size:12.5px;margin-top:14px;white-space:pre-wrap}
</style>
<body>
<div class="wrap">
  <div class="logo"><i></i><i></i><i></i><i></i></div>
  <h1>素材坞</h1>
  <div class="sub" id="sub">正在本地建立索引，只记录位置，不复制、不改动任何文件</div>
  <div class="bar" id="bar"></div>
  <div class="nums">
    <div><div class="k">已扫描</div><div class="v" id="n1">0<em>个文件</em></div></div>
    <div><div class="k">已用时间</div><div class="v" id="n2">0<em>秒</em></div></div>
    <div><div class="k">扫描位置</div><div class="v" id="n3">—</div></div>
  </div>
  <div class="path" id="path"></div>
  <div class="tip">首次启动只扫这一次，之后就是秒开；也可以先最小化，扫完自动进入相册</div>
  <div class="err" id="err"></div>
</div>
<script>
  const el = id => document.getElementById(id);
  let started = false, errorShown = false;
  const num = n => (n || 0).toLocaleString();
  function render(p){
    started = true;
    el('n1').innerHTML = num(p.scanned) + '<em>个文件</em>';
    el('n2').innerHTML = Math.round(p.elapsed || 0) + '<em>秒</em>';
    el('n3').textContent = p.roots ? (p.ri + '/' + p.roots) : '—';
    el('path').textContent = p.msg || '';
    if (p.phase === 'ready'){
      el('bar').classList.add('done');
      el('sub').textContent = '索引完成，共 ' + num(p.total_db) + ' 个媒体文件，正在打开相册…';
      el('path').textContent = '';
      setTimeout(() => location.replace('/'), 700);
      return true;
    }
    if (p.phase === 'error'){
      el('err').textContent = '扫描出错：' + (p.msg || '未知错误') + '。可以关掉窗口重开，或双击 更新索引.bat 看详细报错。';
      return true;
    }
    return false;
  }
  async function tick(){
    try {
      const r = await fetch('/api/progress', {cache:'no-store'});
      const p = await r.json();
      if (render(p)) return;
      if (!p.phase || p.phase === 'idle') el('sub').textContent = '正在准备扫描…';
    } catch (e) {
      if (!started && !errorShown){ errorShown = true; el('path').textContent = '正在启动本地服务…'; }
    }
    setTimeout(tick, 700);
  }
  tick();
</script>
</body></html>"""


def icon_file():
    p = os.path.join(TOOLS, 'icon.ico')
    if os.path.exists(p):
        return p
    mei = getattr(sys, '_MEIPASS', '')
    if mei:
        q = os.path.join(mei, 'icon.ico')
        if os.path.exists(q):
            return q
    return None


def set_window_icon(title='素材坞', timeout=20):
    """给原生窗口和任务栏换成自己的图标（WebView2 窗体默认是 Python 图标）"""
    if os.name != 'nt':
        return
    try:
        import ctypes
        u = ctypes.windll.user32
        ico = icon_file()
        if not ico:
            return
        hwnd = 0
        t0 = time.time()
        while time.time() - t0 < timeout:
            hwnd = u.FindWindowW(None, title)
            if hwnd:
                break
            time.sleep(0.15)
        if not hwnd:
            return
        IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE, WM_SETICON = 1, 0x10, 0x40, 0x80
        big = u.LoadImageW(None, ico, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
        small = u.LoadImageW(None, ico, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        if big:
            u.SendMessageW(hwnd, WM_SETICON, 1, big)
        if small:
            u.SendMessageW(hwnd, WM_SETICON, 0, small)
    except Exception:
        pass


_MUTEX = []


def acquire_single_instance(name='Local\\AssetDockWindow'):
    """只允许一个窗口实例：已经有一个在跑就返回 False（顺便把它叫到前台）"""
    if os.name != 'nt':
        return True
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.CreateMutexW(None, False, name)
        _MUTEX.append(h)  # 句柄必须留着，否则互斥量会被释放
        if k.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            return False
    except Exception:
        return True
    return True


def focus_existing_window(title='素材坞'):
    if os.name != 'nt':
        return False
    try:
        import ctypes
        u = ctypes.windll.user32
        hwnd = u.FindWindowW(None, title)
        if not hwnd:
            return False
        u.ShowWindow(hwnd, 9)       # SW_RESTORE
        u.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def close_splash(delay=1.2):
    """打包时用 --splash 的启动图，主窗口出来后关掉它（源码运行没有这个模块）"""
    def _close():
        try:
            import pyi_splash
            pyi_splash.close()
        except Exception:
            pass
    threading.Timer(delay, _close).start()


HOTKEY_READY = {'ok': False, 'msg': ''}


def register_hotkey(callback, key=0x20, mods=0x0003):
    """全局 Ctrl+Alt+Space：唤起素材坞并把焦点放到搜索框"""
    if os.name != 'nt':
        return False

    def loop():
        try:
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32
            u.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
            u.RegisterHotKey.restype = wintypes.BOOL
            if not u.RegisterHotKey(None, 917, mods, key):
                HOTKEY_READY.update(ok=False, msg='快捷键被其他程序占用')
                return
            HOTKEY_READY.update(ok=True, msg='Ctrl+Alt+Space 已启用')
            msg = wintypes.MSG()
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == 0x0312:  # WM_HOTKEY
                    try:
                        callback()
                    except Exception:
                        pass
        except Exception as e:
            HOTKEY_READY.update(ok=False, msg=str(e))

    threading.Thread(target=loop, daemon=True).start()
    return True


def gui():
    """原生窗口模式：不开浏览器、不占命令行窗口"""
    if not acquire_single_instance():
        focus_existing_window()
        print('素材坞已经在运行，已切回原窗口')
        return
    try:
        import webview
    except Exception as e:
        # 没装 WebView2 之类的情况，退回浏览器
        url, _h, _s = start_http()
        webbrowser.open(url)
        print('GUI 组件不可用（%s），已改用浏览器打开：%s' % (e, url))
        threading.Event().wait()
        return
    # 先起服务、再建窗口：有索引时直接进相册，省掉“加载页再跳转”的一次等待
    try:
        url, _h, scanning = start_http(quiet=True, defer_scan=True)
        start_url = url + ('loading' if scanning else '')
    except Exception as e:
        url = 'about:blank'
        start_url = 'data:text/html,' + quote('<body style="background:#0f1116;color:#e8ecf4;font-family:sans-serif;padding:40px">'
                                              '<h3>启动失败</h3><pre style="color:#e96">%s</pre></body>' % e)
        scanning = False
    win = webview.create_window('素材坞', start_url, width=1440, height=920,
                                min_size=(960, 640), background_color='#0f1116')

    def hotkey():
        focus_existing_window('素材坞')
        try:
            win.evaluate_js('focusSearchFromHotkey && focusSearchFromHotkey()')
        except Exception:
            pass

    register_hotkey(hotkey)
    close_splash()
    threading.Thread(target=set_window_icon, daemon=True).start()
    webview.start()


def auto_scan_loop(interval=900):
    """服务在跑的时候定时补扫，新出的素材自动进库"""
    time.sleep(120)
    while True:
        try:
            print('· 自动补扫…')
            do_scan()
        except Exception as e:
            print('  补扫失败：%s' % e)
        time.sleep(interval)


def main():
    # PPT 转 PDF 的独立子进程入口：只跑 COM，不开界面，也不碰索引
    if len(sys.argv) >= 4 and sys.argv[1] == '--ppt2pdf':
        try:
            _ppt_to_pdf_direct(sys.argv[2], sys.argv[3])
        except Exception as e:
            try:
                with open(sys.argv[3] + '.log', 'w', encoding='utf-8') as f:
                    f.write(str(e)[:500])
            except Exception:
                pass
            sys.exit(1)
        sys.exit(0)
    args = [a for a in sys.argv[1:]]
    cmd = args[0] if args else 'gui'
    if cmd.startswith('-'):
        args = ['gui'] + args
        cmd = 'gui'
    if cmd.startswith('scan'):
        do_scan(full=('--full' in args))
    elif cmd == 'serve':
        serve(no_open=('--no-open' in args))
    elif cmd == 'gui':
        gui()
    else:
        print(__doc__)


if __name__ == '__main__':
    main()

