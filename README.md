# 媒体库 · Media Library

一个本地媒体索引工具：把电脑上的图片、视频、音频扫成一份 SQLite 索引，用相册的方式浏览，双击就能放大看或在窗口里播放。

**默认只读。** 媒体文件始终留在原来的文件夹里，程序只记录"在哪、多大、什么时候改的"，再生成一份缩略图缓存。只有你主动点右键里的复制 / 剪切 / 重命名 / 删除，才会操作真实文件；删除走回收站，可还原。

![界面预览](icon_256.png)

## 下载（Windows 免安装）

到 [Releases · v1.2](../../releases/tag/v1.2) 下载 `media-library-win64-v1.2.zip`（约 67 MB），解压到一个固定目录，双击 `媒体库.exe` 即可。不需要装 Python，首次启动会先扫盘并显示进度。

## 功能

- **相册式首页**：最近 7 天 / 最近 30 天 / 全部 / 图片 / 视频 / 音频 / 动图，以及按来源自动分的相册
- **两层分类**：先按大分类（游戏素材、产品物料、聊天图片、下载、截图…），再按项目内细分（背景、道具、动作帧、AI 生图…）；规则都在 `库服务.py` 的 `CAT_RULES` / `sub_of()` 里，可按自己的目录改
- **筛选与搜索**：类型、分类、位置、细分叠加筛选，按文件名或完整路径搜索
- **无限滚动**：按年月分段，每页 120 条，滚到底自动续
- **内置图片查看器**：双击打开，滚轮缩放、拖动平移、适应窗口 / 1:1、上一张 / 下一张、键盘快捷键
- **内置播放器**：视频、音频双击在窗口里播，进度条可拖动（服务端支持 HTTP Range）
- **非 MP4 视频自动转码**：mov / mkv / avi / flv / wmv / mpg / ts 第一次打开时用内置 ffmpeg 转成 H.264 + AAC 的 MP4（最长边 1920），转完缓存，之后秒开；转码时有进度提示
- **右键菜单**：查看 / 播放、用系统程序打开、在资源管理器定位、复制文件、剪切文件、重命名、删除到回收站、复制完整路径、复制文件名、属性
- **文件操作**：复制 / 剪切写入 Windows 剪贴板（资源管理器里可直接粘贴，剪切带移动效果）；重命名只改当前这一层文件名，自动保留扩展名；删除走回收站
- **界面缩放**：Ctrl + 滚轮 50%~300%，右下角有百分比滑块，比例本地记忆
- **零黑窗**：视频首帧用内置 ffmpeg 抽取，`CREATE_NO_WINDOW` 静默执行
- **单实例**：重复启动只会把已有窗口叫到前面
- **首扫进度页**：显示已扫文件数、用时、第几个盘、当前目录

## 支持格式

| 类型 | 格式 |
| --- | --- |
| 图片（直接显示） | jpg / jpeg / png / gif / webp / bmp / svg / avif / ico |
| 图片（服务端转码后显示） | tif / tiff / psd / heic / heif / jxl |
| 视频 | mp4 / mov / mkv / webm / avi / m4v / flv / wmv / mpg / mpeg / ts（按首字节过滤 TypeScript 源码） |
| 音频 | mp3 / wav / m4a / ogg / flac / aac / wma / opus |

超过 150 MB 的图片不做内嵌解码，会提示改用系统程序打开。

## 快速开始（源码运行）

需要 Windows 10/11、Python 3.10+（开发时用的是 3.13）。

```bash
python -m venv _工具/py
_工具/py/Scripts/pip install -r requirements.txt

# 起原生窗口（WebView2）
_工具/py/Scripts/python 库服务.py gui

# 或者只起本地服务，用浏览器打开 http://127.0.0.1:8765/
_工具/py/Scripts/python 库服务.py serve
```

第一次启动会自动扫描并显示进度页。之后每 15 分钟自动补扫一次。

Windows 也可以直接双击 `启动媒体库.bat`（会使用 `_工具\py` 里的解释器）。

## 扫描范围与跳过

复制示例文件后按需修改，一行一个：

```bash
copy 扫描范围.example.txt 扫描范围.txt
copy 跳过目录.example.txt 跳过目录.txt
```

- `扫描范围.txt`：从这些目录开始递归。不写则默认所有本地固定硬盘 + 当前用户目录。
- `跳过目录.txt`：路径里含这些关键词的整棵目录跳过（Windows、Program Files、node_modules、微信/QQ 缓存等）。

## 数据都在哪

| 路径 | 内容 |
| --- | --- |
| `00_索引/library.db` | SQLite 索引（WAL） |
| `00_索引/缩略图/` | 360px 缩略图缓存，可随时删 |
| `00_索引/预览缓存/` | psd/heic 等转码后的 PNG，可随时删 |
| `00_索引/播放缓存/` | 非 MP4 视频转出来的 MP4，30 天没播自动清，可随时删 |

`00_索引` 只在本机，**不要提交到 git**（里面有完整本地路径）。`.gitignore` 已经排除。

## 打包成 exe

```bash
pyinstaller --noconfirm --clean --onedir --noconsole ^
  --icon icon.ico --name 媒体库 ^
  --add-data "app.html;." --add-data "icon.ico;." ^
  --collect-all webview --collect-all imageio_ffmpeg --collect-all pillow_heif ^
  --collect-submodules pythonnet --hidden-import clr --hidden-import clr_loader --hidden-import cffi ^
  库服务.py
```

成品在 `dist/媒体库/`，整个文件夹拷走即可运行，不需要装 Python。

## 已知限制

- 仅支持 Windows（窗口依赖 WebView2；服务端本身是纯 Python）
- 复制 / 剪切只写剪贴板，不搬文件；剪切后在资源管理器粘到别处，库里的旧路径要等下一次补扫（≤15 分钟）才更新
- 删除是移到回收站，不是永久删除；要彻底删请在回收站里清空
- 首次全盘扫描耗时取决于磁盘文件量，之后都是增量
- `psd` 只读合成图，不解析图层
- 分类靠路径关键词，目录命名不规范时会落到「其他」

## License

MIT，见 [LICENSE](LICENSE)。
