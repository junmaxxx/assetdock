# -*- coding: utf-8 -*-
"""
素材坞 · 全机素材索引
只读扫描磁盘上的图片 / 视频 / 音频，建索引并起一个本地网页，支持分类、搜索、缩略图预览、直接打开原文件。
不复制、不移动、不删除任何用户文件。

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
TOOLS = os.path.join(BASE, '_工具')
APP_HTML = os.path.join(TOOLS, 'app.html')


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
    ('茶饮工作', ('\\品牌素材\\', '\\烘焙素材\\', '\\茶饮',
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
    return None


def guess_cat(path, media):
    low = path.lower()
    for cat, hints in CAT_RULES:
        for h in hints:
            if h in low:
                return cat
    if media == '视频':
        return '视频'
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
    global _MIGRATED
    if not _MIGRATED:
        con.executescript(SCHEMA)
        cols = [r[1] for r in con.execute('PRAGMA table_info(items)')]
        if 'sub' not in cols:
            con.execute("ALTER TABLE items ADD COLUMN sub TEXT DEFAULT ''")
        con.execute('CREATE INDEX IF NOT EXISTS idx_sub ON items(sub)')
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
  seen_run INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_media ON items(media);
CREATE INDEX IF NOT EXISTS idx_cat ON items(cat);
CREATE INDEX IF NOT EXISTS idx_loc ON items(loc);
CREATE INDEX IF NOT EXISTS idx_name ON items(name);
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


def thumb_for(item_id, path, mtime, media='图片', ext=''):
    key = '%d_%d.webp' % (item_id, mtime)
    out = os.path.join(THUMB_DIR, key[:2], key)
    if os.path.exists(out):
        return out
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
    if p.get('sub'):
        where.append('sub=?'); args.append(p['sub'])
    if p.get('q'):
        where.append('(name LIKE ? OR path LIKE ?)')
        args += ['%' + p['q'] + '%', '%' + p['q'] + '%']
    if p.get('ext'):
        where.append('name LIKE ?'); args.append('%' + p['ext'])
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
               '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '22',
               '-vf', 'scale=min(1920\\,iw):min(1920\\,ih):force_original_aspect_ratio=decrease:force_divisible_by=2',
               '-pix_fmt', 'yuv420p',
               '-c:a', 'aac', '-b:a', '128k', '-ac', '2',
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


def play_state(item_id, path, mtime, ext):
    """返回 {'mode': direct/ready/transcoding/error, 'url','pct','msg'}"""
    if ext in DIRECT_VIDEO:
        _dur, vc = _probe_video(path)
        if (not vc) or vc in _VID_OK:
            return {'mode': 'direct', 'url': '/file/%d' % item_id}
    out = play_cache_path(item_id, mtime)
    if os.path.exists(out) and os.path.getsize(out) > 1024:
        return {'mode': 'ready', 'url': '/play/%d' % item_id}
    job = PLAY_JOBS.get(item_id)
    if job is None:
        dur, _vc = _probe_video(path)
        _clean_play_cache()
        job = {'state': 'running', 'pct': 0, 'msg': '正在转码', 't0': time.time()}
        PLAY_JOBS[item_id] = job
        threading.Thread(target=_run_transcode, args=(item_id, path, out, dur),
                         daemon=True).start()
    if job.get('state') == 'error':
        return {'mode': 'error', 'msg': job.get('msg') or '转码失败'}
    if job.get('state') == 'ready':
        return {'mode': 'ready', 'url': '/play/%d' % item_id}
    return {'mode': 'transcoding', 'pct': job.get('pct', 0), 'msg': job.get('msg', '正在转码')}


# ---------------- HTTP ----------------
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
            self.api_facets()
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
            self.api_playprep(int(m.group(1)))
            return

        m = re.match(r'^/api/open/(\d+)$', p)
        if m:
            self.api_open(int(m.group(1)), 'open')
            return

        m = re.match(r'^/api/reveal/(\d+)$', p)
        if m:
            self.api_open(int(m.group(1)), 'reveal')
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

        self._send(404, 'not found', 'text/plain; charset=utf-8')

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        # 打开/定位这类动作走 POST，复用 GET 的分发
        self.do_GET()

    def api_facets(self):
        con = connect()
        total = con.execute('SELECT COUNT(*) FROM items').fetchone()[0]
        media = dict(con.execute('SELECT media, COUNT(*) FROM items GROUP BY media').fetchall())
        cats = con.execute('SELECT cat, COUNT(*) c FROM items GROUP BY cat ORDER BY c DESC').fetchall()
        locs = con.execute('SELECT loc, COUNT(*) c FROM items GROUP BY loc ORDER BY c DESC LIMIT 60').fetchall()
        subs = con.execute("SELECT cat, sub, COUNT(*) c FROM items WHERE sub<>'' "
                           "GROUP BY cat, sub ORDER BY c DESC LIMIT 80").fetchall()
        con.close()
        self._json({'total': total, 'media': media,
                    'cats': [{'k': k, 'n': n} for k, n in cats],
                    'locs': [{'k': k, 'n': n} for k, n in locs],
                    'subs': [{'cat': c, 'k': s, 'n': n} for c, s, n in subs]})

    def api_items(self, q):
        def gv(k, d=''):
            return (q.get(k) or [d])[0]

        p = {'type': gv('type', '全部'), 'cat': gv('cat', '全部'), 'loc': gv('loc', '全部'),
             'sub': gv('sub', ''), 'q': gv('q', '').strip(), 'ext': gv('ext', ''), 'recent': gv('recent', '')}
        sort = gv('sort', 'time')
        try:
            offset = max(0, int(gv('offset', '0')))
            limit = min(200, max(1, int(gv('limit', '120'))))
        except ValueError:
            offset, limit = 0, 120

        wsql, args = _filter(p)
        orders = {'name': 'name COLLATE NOCASE', 'time': 'mtime DESC', 'size': 'size DESC', 'path': 'path COLLATE NOCASE'}
        order = orders.get(sort, orders['time'])

        con = connect()
        total = con.execute('SELECT COUNT(*) FROM items ' + wsql, args).fetchone()[0]
        rows = con.execute(
            'SELECT id,name,ext,media,size,mtime,loc,cat,w,h,path,sub FROM items ' + wsql +
            ' ORDER BY ' + order + ' LIMIT ? OFFSET ?', args + [limit, offset]).fetchall()
        con.close()
        items = [{'id': r[0], 'name': r[1], 'ext': r[2], 'media': r[3], 'size': r[4],
                  'mtime': r[5], 'loc': r[6], 'cat': r[7], 'w': r[8], 'h': r[9], 'path': r[10],
                  'sub': r[11] or ''}
                 for r in rows]
        self._json({'total': total, 'items': items, 'offset': offset})

    def api_item(self, item_id):
        con = connect()
        r = con.execute('SELECT id,name,ext,media,size,mtime,loc,cat,w,h,path,sub FROM items WHERE id=?',
                        (item_id,)).fetchone()
        con.close()
        if not r:
            self._json({'error': 'not found'}, 404)
            return
        self._json({'id': r[0], 'name': r[1], 'ext': r[2], 'media': r[3], 'size': r[4], 'mtime': r[5],
                    'loc': r[6], 'cat': r[7], 'w': r[8], 'h': r[9], 'path': r[10], 'sub': r[11] or ''})

    def api_albums(self):
        con = connect()
        cats = con.execute('SELECT cat, COUNT(*) c FROM items GROUP BY cat ORDER BY c DESC').fetchall()
        specs = [
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
        self._json({'albums': out})

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

    def api_playprep(self, item_id):
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
            st = play_state(item_id, path, mtime, ext)
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
    row = _row_path(item_id)
    if not row:
        self._json({'ok': False, 'msg': '没有这条记录'}, 404)
        return
    path, name = row[0], row[1]
    if not os.path.exists(path):
        con = connect()
        con.execute('DELETE FROM items WHERE id=?', (item_id,))
        con.commit(); con.close()
        self._json({'ok': False, 'msg': '文件已不在原位，已清掉这条索引'}, 404)
        return
    ok, msg = recycle_file(path)
    if not ok:
        self._json({'ok': False, 'msg': msg}, 500)
        return
    con = connect()
    con.execute('DELETE FROM items WHERE id=?', (item_id,))
    con.commit(); con.close()
    self._json({'ok': True, 'msg': msg, 'name': name})


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
    """看看是不是已经有素材库在跑，有就返回它的地址"""
    for port in range(8765, 8786):
        url = 'http://127.0.0.1:%d/' % port
        try:
            with urllib.request.urlopen(url + 'api/facets', timeout=0.5) as r:
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


def scan_in_thread():
    """后台扫描，进度写进 PROGRESS，界面上的进度条靠它刷新"""
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
    win = webview.create_window('素材坞', html=LOADING_HTML, width=1440, height=920,
                                min_size=(960, 640), background_color='#0f1116')

    def boot():
        try:
            url, _h, scanning = start_http(quiet=True, defer_scan=True)
            win.load_url(url + ('loading' if scanning else ''))
            close_splash()
        except Exception as e:
            try:
                win.load_html('<body style="background:#0f1116;color:#e8ecf4;font-family:sans-serif;padding:40px">'
                              '<h3>启动失败</h3><pre style="color:#e96">%s</pre></body>' % e)
            except Exception:
                pass

    threading.Thread(target=boot, daemon=True).start()
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
