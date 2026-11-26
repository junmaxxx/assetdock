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
import json
import time
import math
import uuid
import shutil
import socket
import hashlib
import sqlite3
import subprocess
import webbrowser
import threading
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote

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
    for d in (IDX, THUMB_DIR):
        os.makedirs(d, exist_ok=True)


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
        }
        for name, ddl in extra.items():
            if name not in cols:
                con.execute('ALTER TABLE items ADD COLUMN %s %s' % (name, ddl))
        con.execute('CREATE INDEX IF NOT EXISTS idx_sub ON items(sub)')
        con.execute('CREATE INDEX IF NOT EXISTS idx_sha ON items(sha)')
        con.execute('CREATE INDEX IF NOT EXISTS idx_size ON items(size)')
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
            'sub=excluded.sub, seen_run=excluded.seen_run',
            batch)
        con.commit()
        batch.clear()

    for ri, root in enumerate(roots, 1):
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


def play_state(item_id, path, mtime, ext, allow_transcode=True):
    """返回 {'mode': direct/ready/transcoding/unsupported/error, 'url','pct','msg','reason'}"""
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
        return {'id': item_id, 'ok': False, 'name': name, 'msg': '文件已不在原位，已清掉索引'}
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
    if moved:
        bump_data()
    self._json({'ok': True, 'moved': len(moved), 'failed': failed, 'batch': batch,
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
        con.execute('UPDATE items SET path=?,name=?,ext=?,size=?,mtime=?,cat=?,sub=? WHERE id=?',
                    (target, os.path.basename(target), n_ext, st.st_size, int(st.st_mtime),
                     guess_cat(target, media_of(n_ext)), sub_of(target, media_of(n_ext)), item_id))
        done.append({'id': item_id, 'name': os.path.basename(target)})
    con.commit(); con.close()
    if done:
        bump_data()
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

        self._send(404, 'not found', 'text/plain; charset=utf-8')

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        # 打开/定位这类动作走 POST，复用 GET 的分发
        self.do_GET()

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
                'trash': _trash_count()}
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
    con = connect()
    con.execute('UPDATE items SET path=?, name=?, ext=?, size=?, mtime=?, cat=?, sub=?, w=NULL, h=NULL WHERE id=?',
                (target, new_name, n_ext, st.st_size, int(st.st_mtime),
                 guess_cat(target, media_of(n_ext)), sub_of(target, media_of(n_ext)), item_id))
    con.commit(); con.close()
    bump_data()
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

