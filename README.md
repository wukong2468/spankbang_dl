# spankbang-dl

> ## ⚠️ 免责声明
>
> **本项目仅供个人学习与技术研究使用，严禁用于任何违法用途。**
>
> - 使用者须自行确保其行为符合所在地法律法规；因使用本项目产生的**一切后果由使用者自负**，
>   作者不承担任何责任。
> - 本项目只是一个**通用下载工具**，不提供、不存储、不传播任何内容。
> - 下载所得内容**仅供个人私下观看**，请勿再上传或分发。
> - 目标站点为**成人内容网站**，未满 18 岁或所在地法律禁止者不得使用。
>
> **继续使用即表示你已阅读、理解并同意下方 [完整免责声明](#免责声明disclaimer) 的全部条款。**
> 如不同意任何一条，请立即停止使用并删除本项目。

批量采集 SpankBang 的**播放列表**和**搜索列表**，解析每个视频的最高清晰度直链，
并行下载到本地。

## 特性

- **一个 id 定身份**：视频的身份就是 `stream id`，从列表页条目的 `data-id` 直接读到，
  并写进 MP4 内嵌标签。已下载的**连视频页都不打开、直链也不解析**，直接跳过。
- **两类列表都支持**：播放列表页和搜索列表页共用同一套采集逻辑。
- **多个列表并行**：一次给多个网址，多个 Chrome 实例同时翻页采集，按 id 跨列表去重。
- **递归扫描**：保存目录下所有子目录里的视频都会认出来（归类到子文件夹也不怕）。
- **一份已下载账本**：`已下载完成.json` 记住「这些 id 我拿到过」，文件被移走也不重下。
- **自带下载器**：直接用 HTTP 拉取直链，多线程并行，支持断点续传。
- **大文件自动分段并发**：后期只剩少量视频时，单条直链自动切成小块并发拉，
  而且并发数**动态涨落**——别的视频下完空出带宽就自动加，来新视频就自动让。
  实测同一时刻对比：分段稳定 ~90 MB/s，单连接 44~62 MB/s。
- **只下 MP4**：渐进式 MP4 优先；没有 MP4 档位的视频直接报错跳过，
  不会产出一个「下完了却认不出来、每次重下」的文件。
- **Cloudflare 处理**：持久化 profile + 有头真实 Chrome，必要时自动点 Turnstile 复选框。
- **Ctrl+C 立刻生效**：中断信号直达下载线程，收尾有上限，不会被卡住。

## 安装

需要 [uv](https://docs.astral.sh/uv/) 和系统已安装的 Google Chrome。

```bash
uv sync
```

## 打包成 exe

不想装 Python 环境的话，可以打包成一个**免安装目录**分发给别人。

```bash
uv sync                                    # 包含 dev 依赖（pyinstaller）
uv run pyinstaller spankbang-dl.spec --noconfirm --clean
```

产物在 `dist/spankbang-dl/`：

```
dist/spankbang-dl/
├── spankbang-dl.exe      3.4 MB     ← 双击这个
└── _internal/            126 MB     ← 依赖都在这儿，整个文件夹一起拷
                          129.5 MB 合计
```

### 为什么是这个体积

| 部分 | 大小 | 说明 |
|---|---|---|
| **Playwright 的 Node 驱动** | 101 MB | 必需。驱动真实 Chrome 的那层 |
| Python 运行时 + 标准库 | ~20 MB | |
| pathvalidate / mutagen / 本项目 | ~2 MB | |

**Playwright 自带的浏览器（chromium/firefox/webkit，1.1 GB）没有打包** ——
本项目用 `channel="chrome"` 驱动**系统装的 Google Chrome**，所以一个字节都不用带。

### ⚠️ 分发时要注意

1. **目标机器必须装了 Google Chrome。** 没装的话会在启动浏览器时报错，
   脚本会提示「请确认系统装了 Google Chrome」。

2. **必须整个 `dist/spankbang-dl/` 文件夹一起拷贝**，不能只拿那个 exe。

3. **为什么是文件夹而不是单个 exe**：如果用 onefile，每次启动都要把 129 MB
   解压到 `%TEMP%\_MEIxxxx`（慢 3~8 秒），而且 Playwright 要拉起 Node 驱动的
   子进程，路径每次都变容易出问题。onedir 启动快、也稳。

4. 首次运行会在**当前工作目录**下建 `profiles/`（浏览器 profile，体积不小）
   和 `Downloads/`（下载目录）。双击运行时工作目录就是 exe 所在目录。

### 改了代码之后重新打包

`--clean` 已经清掉上次的缓存，直接重跑上面那条命令即可。

### 打包相关的文件

| 文件 | 作用 |
|---|---|
| `spankbang-dl.spec` | PyInstaller 配置（收集 playwright 驱动、排除用不到的东西） |
| `packaging/entry.py` | exe 的入口。用绝对导入，因为 PyInstaller 把入口脚本当顶层模块 |
| `console.setup_console()` | 把控制台代码页切成 UTF-8，否则中文是乱码 |


## 使用

```bash
# 直接跑：会提示你输入列表网址
uv run spankbang-dl

# 一个列表
uv run spankbang-dl --playlist "https://spankbang.com/xxxx/playlist/yyy/"

# 多个列表：空格分隔（播放列表和搜索列表可以混着来）
uv run spankbang-dl --playlist "https://spankbang.com/chzqj/playlist/new/ https://spankbang.com/s/vam/"

# 也可以重复给 --playlist
uv run spankbang-dl --playlist "https://a.com/s/x/" --playlist "https://b.com/s/y/"

# 加大浏览器并发
uv run spankbang-dl --resolvers 12

# 调高总连接上限（视频少时会自动切成更多段）
uv run spankbang-dl --workers 64

# 关掉分段下载（机械盘上可能反而更慢）
uv run spankbang-dl --no-segment

# 只在文件大于 100 MB 时才分段，且最多切 16 段
uv run spankbang-dl --segment-min 100 --segments 16

# 先试跑，只看解析结果
uv run spankbang-dl --dry-run --limit 3

# 只列出直链，不下载
uv run spankbang-dl --list-only

# 指定输出目录
uv run spankbang-dl --playlist "https://spankbang.com/xxxx/playlist/yyy/" --outdir "D:\Videos"
```

### 支持的列表类型

```
播放列表   https://spankbang.com/ceha4/playlist/asian+baddies/
           https://spankbang.com/chzqj/playlist/new/
           https://spankbang.com/cxc84/playlist/favorites/

搜索列表   https://spankbang.com/s/vam/
           https://zh.spankbang.com/s/%E5%90%8E%E5%85%A5/
```

两类页面的**条目容器和链接形式都不一样**（搜索页没有 `div.main_results`，
而且条目是直接指向视频页的），但条目元素都带 `.js-video-item`、
都带 `data-id`、分页都是 `.pagination li.next`，所以采集只认这三点，
两类页面用同一套逻辑。

> 注意：顶部导航里也塞了 8 个同类元素，采集时会按 `header` 祖先排除掉。
> `zh.spankbang.com` 只是把界面语言换成中文，和 `spankbang.com` 是同一个站，
> **Cloudflare 检测没有区别**，哪个顺手用哪个就行。

### 交互式输入

不带 `--playlist` 时会在控制台提示输入。可以一行塞多个（空格或逗号分隔），
也可以一个一个来；**直接回车结束**，输入 `q` 退出：

```
  请输入列表网址
    播放列表页和搜索列表页都行，例如：
        https://spankbang.com/5868a/playlist/favorites/
        https://spankbang.com/s/vam/

    可一次输入多个（用空格分隔），也可以一个一个来：输一个回车一次
    直接回车结束输入；输入 q 退出
  > https://spankbang.com/chzqj/playlist/new/
  > (1 个已收) https://spankbang.com/s/vam/
  > (2 个已收)
  共收到 2 个网址
```

输入容错：去掉粘贴时带的引号；漏写 `https://` 会自动补上；重复的网址会合并。

## 参数

| 参数 | 说明                                                   |
|---|--------------------------------------------------------|
| `--playlist` / `--list` | 列表网址，**可以给多个**（空格分隔）；不指定则提示输入 |
| `--outdir` | 下载保存目录（默认 `./Downloads`）                     |
| `--headless` | 无头模式。**本站在无头下会被 Cloudflare 拦，默认有头** |
| `--resolvers` | 并行浏览器进程数（采集和解析共用，默认 10）            |
| `--workers` | **全局并发连接上限**（默认 50）。视频多时等于「同时下几个」，视频少时会自动把单条直链切成多段来吃满带宽 |
| `--segments` | 单个文件最多切几段（默认 32）；实际段数还会按当时在下载的视频数均分 |
| `--segment-min` | 小于这个大小就不分段（单位 MB，默认 32）               |
| `--no-segment` | 关掉分段下载，每条直链只用一条连接                     |
| `--prefer` | 优先清晰度，缺失时向下回退。**只影响新视频**           |
| `--start` / `--limit` | 处理区间，便于分批跑                                   |
| `--dry-run` / `--list-only` | 试运行 / 只列直链                                      |
| `--manifest` | 运行清单文件名（默认 `下载清单.json`）                 |
| `--done-file` | 账本文件名（默认 `已下载完成.json`），放在保存目录里   |
| `--ignore-done` | 忽略账本，只用扫描到的文件判断（删了文件就会被重下）   |
| `--profile-dir` | 浏览器 profile 目录（每个进程一份）                    |
| `--tmp-dir` | Playwright 临时目录（默认自动挑可写的位置）            |
| `-v` | 输出调试日志                                           |

## 身份模型（核心）

**唯一标识就是 `stream id`**，例如 `15964832`。

它是从列表页条目的 `data-id` 直接读到的：

```html
<div class="js-video-item" data-id="16074294" ...>
```

实测 `data-id` 与直链里的 `_tid` 完全一致（3/3），而且**两种列表页上都有** ——
所以不需要打开视频页就能知道身份。

### 内嵌进视频的两个标签

```
----:spankbang-dl:stream_id  =  16074294
----:spankbang-dl:source     =  https://vdownload-45.sb-cd.com/1/6/16074294-4k.mp4
```

（另有一条 `©cmt` 写「来源: …」方便人看。）

MP4 自定义 atom 的格式是 `----:<mean>:<name>`。`mean` 按惯例写成反向域名
（如 `com.apple.iTunes`）只是为了**避免不同软件之间撞名，并不是格式要求** ——
实测 `spankbang-dl` 这样的短名字同样能正常写入读回，所以就不加 `com.` 了。

`source` 存的是**去掉签名查询串**的规范直链 —— 带签名那段 12 小时就失效，存了没意义。
想从里面看实际下到的分辨率，看 `-4k` / `-1080p` 那一段即可（清单里也会显示，
但**纯粹是展示，不参与判断**）。

这些都是 iTunes 风格的自定义 atom，播放器会忽略，不影响播放。

### 判定规则

「已经有什么」由**两处合并**而来：

| 来源 | 说明 |
|---|---|
| ① **递归扫描保存目录** | 读每个视频内嵌的 `stream_id`。**会遍历所有子目录** —— 下载好的视频常被手动归类到子文件夹里，只扫顶层会漏掉。 |
| ② **`已下载完成.json`** | 一份跨运行累积的账本，补上「文件已经被移走或删掉」的情况。 |

```
列表条目的 data-id 命中任一来源  →  跳过（不开视频页、不解析直链）
都不命中                        →  打开视频页 → 解析直链 → 下载 → 写入 id + 直链
```

**不比较分辨率。** 同一个视频不会因为站点后来提供了更高清晰度而重下；
想换清晰度就删掉文件重下。`--prefer` 只影响新视频下哪一档。

### ⚠️ 删掉文件不会重下

只要 id 还记在 `已下载完成.json` 里，**删掉文件也不会重下**。
想重下必须**同时**做两件事：

1. 删掉视频文件
2. 把 `已下载完成.json` 里对应的那一条也删掉

只想按文件判断（删了就重下），加 `--ignore-done`。

### 只下 MP4，不做 HLS 回退

`stream_data` 里同时有渐进式 MP4 档位、`main`、HLS（`m3u8`）和 DASH（`mpd`）。
本工具**只取 MP4**：

```
① 4k / 1080p / 720p / 480p / 320p / 240p   ← 都是 .mp4
② stream_data.main                          ← 也是 .mp4
③ 没有 MP4 可用 → 报错跳过该视频，不下载
```

原因是 HLS 分片拼出来的文件不是 MP4（而且这里的 m3u8 是 `.mp4.urlset`，
分片很可能是 fMP4 片段，直接拼接既不是合法 TS 也不是合法 MP4）。
那种文件写不进内嵌身份标签 → 下次扫描认不出来 → **每次运行都会重下**。
所以宁可跳过并报错：

```
  × 解析失败  该视频没有 MP4 档位（只有 HLS 流），已跳过  <标题>
```

实测 5 个真实视频都同时有命名档位和 `main`，所以这条路走不到；
留下它是为了在遇到异常视频时给出明确提示，而不是悄悄产出一个管不了的文件。

## 分段下载（大文件提速）

**问题**：批量下载时一条直链只开一条连接。视频多的时候并发够用，但
**后期只剩几个大文件**时带宽就吃不满。

**做法**：把单条直链切成很多**小块**，一个监管协程按当时的空闲情况
决定同时跑几块，并发数**动态涨落**。

### 为什么要「动态」而不是一开始切好

早先的实现是按开始那一刻的情况把段数定死。但如果一开始有 100 个视频在下载，
每条只能分到 1 路；等小视频都下完、带宽空出来了，剩下那几个大视频**仍然只有
1 路**，白白闲着。

所以改成「分块队列 + 动态并发」：

```
把文件按 8 MB 切成很多块
每 2 秒重算一次「这条直链现在该占几条连接」：

    目标 = max(1, min(--segments, --workers // 当前在下载的视频数))

在跑的块任务不够就再起几个，够了就不起
```

实测（模拟「一开始 50 个视频，然后只剩 1 个」）：

```
起始 active=50 -> 目标并发 = 1
  >>> 小视频下完了，active 降到 2 -> 目标并发 = 25

t=   0s   峰值并发=  0   active=50     ← 只有 1 路
t=   5s   峰值并发= 24   active=2      ← 自动涨到 24 路
```

并发数随时间自动涨落，全程贴合实际负载。

以默认的 `--workers 50 --segments 32` 为例：

| 同时在下载 | 每个文件几路 |
|---|---|
| 1 个 | **32** |
| 2 个 | 25 |
| 5 个 | 10 |
| 16 个 | 3 |
| 50 个以上 | 1（等于以前的单连接） |

**视频多的时候行为完全不变**，只有视频少了才自动提速。

### 实测效果

⚠️ **这个 CDN 的吞吐波动很大**，同一方法在不同时刻能差好几倍（实测单连接
在 10~62 MB/s 之间跳），所以「先后各跑一次再比较」是没有意义的。

有意义的做法是**同一时刻并行跑两个下载**，看谁抢到多少带宽。同一个 582 MB
文件，交替测了 3 轮：

```
             单连接(1 路)        分段(24 路)
第 1 轮      13.1s  44.4 MB/s    6.4s  91.1 MB/s
第 2 轮       9.4s  61.9 MB/s    6.4s  91.3 MB/s
第 3 轮      11.0s  52.9 MB/s    6.6s  88.1 MB/s
```

结论：分段稳定在 **~90 MB/s**，单连接在 **44~62 MB/s** 之间抖。
**实际提升约 1.5~2 倍**，主要收益是**快而且稳**。

字节级一致性（这个是确定可验证的）：

```
分段 sha256 : 071f6714def2c6f20bcbeac7f24115004f7192c60ffed5cc36f14c7749658c1a
单连 sha256 : 071f6714def2c6f20bcbeac7f24115004f7192c60ffed5cc36f14c7749658c1a
完全一致    : True
```

### 落盘方式

只建**一个** `.part` 文件，预先撑到完整大小；每块用独立文件句柄 `seek` 到自己的
偏移处顺序写。所以：

- **没有额外 I/O**（不像「每块一个文件再拼接」那样要再读+写一遍）
- **峰值磁盘占用只有 1 份**（2 GB 的文件不需要 4 GB 空间）

### 断点续传

旁边放一个小 json 记录**哪些块已经下完**：

```json
{"total": 610502613, "chunk": 8388608, "done": [0, 1, 2, 5, 7, ...]}
```

- 2 GB / 8 MB = 256 块，记录就是几百个数字
- 每 2 秒落一次盘；中断最多损失「正在下那几块」的进度（每块 ≤ 8 MB）
- 布局对不上（比如改了块大小）就整个重来

实测：

```
中断时已写 69.0 MB
续传后哈希 == 单连接哈希: True
```

### ⚠️ 分段不被支持时自动回退

实测有些 CDN 的 Range 支持**不可靠**——同一个 URL：

```
Range: bytes=0-        -> HTTP 206   ✅
Range: bytes=4194304-  -> HTTP 404   ❌ 同一个文件！
```

所以某一块连续失败 **2 次**就判定这条直链分段不可用，**整体退回单连接**重下：

```
! 分段不被服务器接受，回退单连接，改为单连接重下
```

### 什么情况不分段

| 条件 | 原因 |
|---|---|
| 文件小于 `--segment-min`（默认 32 MB） | 探测 + 多连接的开销不划算 |
| 服务器不支持 Range | 分不了 |
| 只分到 1 路 | 等于单连接，没必要建 json |
| 加了 `--no-segment` | 手动关闭 |

> 没有做「磁盘是不是机械盘」的判断。SSD 上几十路并发写没问题；机械盘上
> 并发随机写**可能反而更慢**，那种情况加 `--no-segment` 或调小 `--segments` 即可。

### 为什么用 id 而不是标题/文件名/视频页网址

- **标题、文件名不可靠**：同一个播放列表里出现过两条标题完全一样的视频，
  也出现过标题自带冒号被截断后撞名的。
- **视频页网址不可靠**：它的路径是 `/{id}/video/{slug}`，而 `slug` 来自标题
  （实测三个完全不同的视频 slug 都是 `porn`），标题一改 slug 就变；
  域名还有 `spankbang.com` / `zh.spankbang.com` 两个。

id 写进文件之后：改文件名不影响、改标题不影响、重名不影响；
**删掉文件 = 身份一起没了 = 下次自动重下**。

## 文件命名
```
基础名 = 列表页上的标题（它本来就是干净的）
若磁盘上已存在 "{基础名}.mp4"  →  "{基础名} - {stream_id}.mp4"
否则                          →  "{基础名}.mp4"
```

例：`chinese.mp4` 已存在，另一条 id 不同但标题也是 `chinese` 的
→ 命名为 `chinese - 222.mp4`。

「磁盘上已存在」是**递归**判断的：下载好的视频常被手动归类到子目录里，
只看顶层的话，归类之后同名的视频又会拿到同一个名字，日后整理时就撞上了。
新文件本身总是落在保存目录的**顶层**。

**刻意不按冒号截断标题。** 视频页标题形如 `标题: 标签1, 标签2 Porn - SpankBang`，
看着可以按第一个冒号切掉标签，但标题本身也可能含冒号，比如：

```
+賴gg9875或TG:@gg9875約妹大奶處女台灣吃魚喝茶汽旅飯店出差叫小姐
+賴gg9875或TG:@gg9875台北台中高雄彰化南投台南新竹全台約妹大奶
```

按冒号切，这两条会变成同一个名字、互相覆盖。

## 中断与续传

- 下载时先写断点文件，**全部下完才改名**成正式文件名，所以保存目录里永远不会
  出现「下了一半却顶着正式名字」的文件。
- 断点文件按**身份**命名，而不是按最终文件名：

  ```
  Huge Dildo 16074294.mp4.part
  ```

  这样即使磁盘上同名文件增删变化、导致最终文件名换了，中断后依然能找到同一个
  `.part` 续传。
- 下次运行会重新解析直链（签名是新的），但断点文件对得上，就从
  `Range: bytes=<已有字节>-` 继续。
- **分段下载**时，断点信息在旁边的 `.part.json` 里，记录每段各自下到哪了，
  每 2 秒落一次盘。布局对得上就按段续传，对不上（比如改了 `--segments`）就整个重来。
- 想重新下载某个视频，删掉文件、并把 `已下载完成.json` 里对应的那条一起删掉。

### Ctrl+C

中断信号会立刻置位一个进程级事件，下载线程在每个数据块之间检查它，所以能马上收手。
之后给协程最多 **8 秒**收尾（关浏览器、写清单），到点还没完就 `os._exit()` 直接退出。
**再按一次 Ctrl+C 可以立刻强制退出**。

> 之所以要这么做：下载跑在线程里，如果某个线程正卡在一次 TCP 连接/读取上
> （socket 超时最长 30 秒），`asyncio.run` 退出时会一直等它，表现就是
> 「按了 Ctrl+C 没反应」。

另外，有头模式会弹出 Chrome 窗口并**可能抢走终端焦点**——按 Ctrl+C 之前请先点一下
终端窗口，否则 Ctrl+C 会发给 Chrome。

## 两个 JSON 文件

| 文件 | 性质 | 参与判定吗 |
|---|---|---|
| `下载清单.json` | **每次运行重写的运行报告**（含本次统计和失败清单） | ❌ 不参与 |
| `已下载完成.json` | **跨运行累积的已下载账本**（只增不删） | ✅ **参与** |

### 已下载完成.json

以 stream id 为键：

```json
{
  "17026318": {
    "分辨率": "1080p",
    "标题": "ourdream POV：丰满亚洲Sunhee棒棒糖闺房深喉",
    "文件名": "ourdream POV：丰满亚洲Sunhee棒棒糖闺房深喉.mp4",
    "来源": "https://vdownload-49.sb-cd.com/1/7/17026318-1080p.mp4",
    "来自列表": "https://zh.spankbang.com/dup8k/playlist/ai+porn",
    "时长": "4:41",
    "文件大小": "193.51 MB"
  }
}
```

写入策略是**合并，不是重建**：以现有内容为底，只新增/更新本次下载的 id，
没碰过的条目原样保留（连字段顺序都不动），所以你手工加过的条目和自定义字段
不会被弄坏。整体按 id 数值升序排列。文件坏了会**报错而不是覆盖**。

文件名由 `--done-file` 指定（默认 `已下载完成.json`），放在保存目录里。

### 时长和文件大小的格式

两个 JSON 里都用**人类可读字符串**，不用裸数字：

```
时长      "4:41"  /  "1:02:03"      （取不到写 "未知"）
文件大小  "193.51 MB"  /  "1.50 GB"
```

## Cloudflare 说明

本站在 Cloudflare 后面。实测结论：

| 组合 | 结果 |
|---|---|
| Playwright + 系统 Chrome，**有头**，持久化 profile | ✅ 直接通过 |
| Playwright + 系统 Chrome，无头 | ❌ 被拦 |
| Camoufox（Firefox），有头 | ❌ 卡在 `Just a moment...` |
| Patchright + Chrome，有头 | ❌ 被拦 |

`zh.spankbang.com` 只是站点的中文界面版本，和 `spankbang.com` 是同一个站，
**Cloudflare 检测上没有区别** —— 两边都可能撞上挑战，也都能靠等待+刷新放行。

所以默认用**真实 Chrome + 有头 + 持久化 profile**。持久化 profile 能把
Cloudflare 放行后种下的 `cf_clearance` Cookie 留下来，第二次访问就不用再挑战。

挑战页常常「其实已经过了但停在原地」，所以脚本每 8 秒主动刷新一次。

> 每个并行浏览器进程必须用**独立的 profile 目录**（`profiles/0`、`profiles/1`…、
> 采集用 `profiles/c1`、`profiles/c2`…），因为 Chrome 会锁住 profile，
> 共用会直接启动失败。新 profile 第一次访问会被挑战一次，属正常。

如果遇到 Turnstile 人机校验控件，脚本会顺着 Shadow DOM 找到复选框并点击。
该思路来自 [cloudflare_captcha_solver](https://github.com/wukong2468/cloudflare_captcha_solver)。

> `zh` 站还有一个「您是否已满18岁？」浮层。实测它**不影响 DOM 读取**
> （条目照样能采到），所以暂时不处理。

## 项目结构

```
src/spankbang_dl/
├── cli.py           命令行入口（多网址参数、交互式输入、事件循环与中断收尾）
├── config.py        全局配置常量
├── console.py       终端输出（中文、配色、进度条、人类可读的时长/大小）
├── cloudflare.py    Cloudflare 挑战检测与处理
├── browser.py       浏览器启动（Playwright + 系统 Chrome）
├── scraper.py       页面操作：采集列表条目、解析 stream_data
├── metadata.py      MP4 内嵌身份标签的读写、递归扫描
├── done.py          已下载完成.json 的读写与合并
├── downloader.py    HTTP 下载（断点续传、分段区间下载、重试退避）
├── naming.py        文件名清洗、直链解析、文件名分配
├── manifest.py      运行清单（以 stream id 为键）
└── pipeline.py      主流程编排（采集池 + 解析池 + 下载池）

packaging/
└── entry.py         exe 打包入口（绝对导入）
```

## 主流程

```
多个列表网址
     │
     ▼
并行采集（N 个浏览器实例，每个列表翻到底）
     │
     ▼
按 data-id 跨列表去重                    ← 一个浏览器都不用开
     │
     ▼
① 递归扫描保存目录（含所有子目录），读每个 MP4 内嵌的 id
② 读 已下载完成.json
     │  两者合并 = 「已经有什么」
     ▼
分流：id 命中 → 跳过
      id 不命中 → 解析直链 → 下载 → 写入 id + 直链
                                          │
                                          ▼
                              追加进 已下载完成.json
```

## 免责声明（Disclaimer）

### 中文

**本项目仅供个人学习、技术研究与技术交流使用。**

1. **禁止任何违法用途。** 不得利用本项目从事任何违反使用者所在国家或地区法律法规的
   活动，包括但不限于：侵犯著作权、传播违法或有害信息、规避技术保护措施、
   未经授权批量获取他人作品等。

2. **本项目只是通用工具。** 它不提供、不存储、不托管、不传播任何音视频内容，
   也不对目标站点的内容做任何筛选或背书。所下载内容的一切权利归原权利人所有。

3. **不得再传播。** 下载所得内容仅供个人私下观看，请勿上传、分享、出售或以任何
   形式再分发。

4. **年龄限制。** 目标站点为成人内容网站，未满 18 周岁或未达所在地法定年龄者
   不得使用本项目。

5. **地域限制。** 该类内容在部分国家和地区属于违法或受限制内容。使用者应在访问前
   自行确认所在地法律是否允许，并承担由此产生的一切后果。

6. **风险自负。** 使用本项目所产生的一切直接或间接后果——包括但不限于账号封禁、
   IP 或设备被限制、网络服务中断、数据或设备损坏、法律纠纷与处罚——均由
   **使用者自行承担**，作者不承担任何责任。

7. **无担保。** 本项目按「现状（AS IS）」提供，不附带任何明示或暗示的担保，
   包括但不限于对适销性、特定用途适用性及非侵权的保证。作者不保证本项目
   可用、无错误或不会造成损失。

8. **作者保留权利。** 作者可随时修改、暂停或终止本项目及本声明，恕不另行通知。

9. **不同意即停止使用。** 如果你不同意上述任何一条，请立即停止使用并删除本项目
   及其全部副本。

### English

**This project is provided for personal study and technical research only.**

1. **No illegal use.** You must not use this project for any activity that violates the
   laws or regulations of your jurisdiction, including but not limited to copyright
   infringement, distributing unlawful content, or circumventing technical protection
   measures.

2. **This is a generic tool.** It does not host, store, provide, or distribute any media
   content, and it neither filters nor endorses the content of any target site. All
   rights to downloaded material remain with the respective rights holders.

3. **Do not redistribute.** Downloaded material is for private, personal viewing only.
   Do not upload, share, sell, or otherwise redistribute it.

4. **Age restriction.** The target site hosts adult content. You must be at least 18 years
   old, or of the legal age in your jurisdiction, to use this project.

5. **Regional restrictions.** Such content may be illegal or restricted in some countries
   and regions. You are responsible for verifying that your use is lawful where you are.

6. **Use at your own risk.** The author accepts **no responsibility or liability** for any
   direct or indirect consequence of using this project, including but not limited to
   account bans, IP or device restrictions, service interruptions, data or hardware
   damage, and legal disputes or penalties.

7. **No warranty.** This project is provided "AS IS", without warranty of any kind,
   express or implied, including but not limited to the warranties of merchantability,
   fitness for a particular purpose, and non-infringement.

8. **Reservation of rights.** The author may modify, suspend, or discontinue this project
   and this disclaimer at any time without notice.

9. **If you disagree, stop using it.** If you do not agree with any of the above, please
   stop using this project immediately and delete it together with all copies.
