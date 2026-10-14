# spankbang-dl

批量采集 SpankBang 的**播放列表**和**搜索列表**，解析每个视频的最高清晰度直链，
并行下载到本地。

## 特性

- **一个 id 定身份**：视频的身份就是 `stream id`，从列表页条目的 `data-id` 直接读到，
  并写进 MP4 内嵌标签。已下载的**连视频页都不打开、直链也不解析**，直接跳过。
- **两类列表都支持**：播放列表页和搜索列表页共用同一套采集逻辑。
- **多个列表并行**：一次给多个网址，多个 Chrome 实例同时翻页采集，按 id 跨列表去重。
- **自带下载器**：直接用 HTTP 拉取直链，多线程并行，支持断点续传。
- **只下 MP4**：渐进式 MP4 优先；没有 MP4 档位的视频直接报错跳过，
  不会产出一个「下完了却认不出来、每次重下」的文件。
- **Cloudflare 处理**：持久化 profile + 有头真实 Chrome，必要时自动点 Turnstile 复选框。
- **Ctrl+C 立刻生效**：中断信号直达下载线程，收尾有上限，不会被卡住。

## 安装

需要 [uv](https://docs.astral.sh/uv/) 和系统已安装的 Google Chrome。

```bash
uv sync
```

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

# 加大并发
uv run spankbang-dl --resolvers 6 --workers 5

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

| 参数 | 说明 |
|---|---|
| `--playlist` / `--list` | 列表网址，**可以给多个**（空格分隔）；不指定则提示输入 |
| `--outdir` | 下载保存目录（默认 `./Downloads`） |
| `--headless` | 无头模式。**本站在无头下会被 Cloudflare 拦，默认有头** |
| `--resolvers` | 并行浏览器进程数（采集和解析共用，默认 4） |
| `--workers` | 并行下载数（默认 3） |
| `--prefer` | 优先清晰度，缺失时向下回退。**只影响新视频** |
| `--start` / `--limit` | 处理区间，便于分批跑 |
| `--dry-run` / `--list-only` | 试运行 / 只列直链 |
| `--profile-dir` | 浏览器 profile 目录（每个进程一份） |
| `--tmp-dir` | Playwright 临时目录（默认自动挑可写的位置） |
| `-v` | 输出调试日志 |

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

| 情况 | 动作 |
|---|---|
| 列表条目的 `data-id` 已在本地 | **跳过** —— 不开视频页、不解析直链 |
| `data-id` 不在本地 | 打开视频页 → 解析直链 → 下载 → 写入 id + 直链 |

**不比较分辨率。** 同一个视频不会因为站点后来提供了更高清晰度而重下；
想换清晰度就删掉文件重下。`--prefer` 只影响新视频下哪一档。

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
- 想重新下载某个视频，直接删掉那个文件即可。

### Ctrl+C

中断信号会立刻置位一个进程级事件，下载线程在每个数据块之间检查它，所以能马上收手。
之后给协程最多 **8 秒**收尾（关浏览器、写清单），到点还没完就 `os._exit()` 直接退出。
**再按一次 Ctrl+C 可以立刻强制退出**。

> 之所以要这么做：下载跑在线程里，如果某个线程正卡在一次 TCP 连接/读取上
> （socket 超时最长 30 秒），`asyncio.run` 退出时会一直等它，表现就是
> 「按了 Ctrl+C 没反应」。

另外，有头模式会弹出 Chrome 窗口并**可能抢走终端焦点**——按 Ctrl+C 之前请先点一下
终端窗口，否则 Ctrl+C 会发给 Chrome。

## 清单

`下载清单.json` 是**身份清单**，以 stream id 为键，把标题、文件名、来源直链、
分辨率（从直链推出来的，仅供查看）、文件大小、下载状态摊开。

**它不参与「要不要下载」的判断** —— 那个判断只看文件内嵌的 id。

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
├── console.py       终端输出（中文、配色、进度条）
├── cloudflare.py    Cloudflare 挑战检测与处理
├── browser.py       浏览器启动（Playwright + 系统 Chrome）
├── scraper.py       页面操作：采集列表条目、解析 stream_data
├── metadata.py      MP4 内嵌身份标签的读写与扫描
├── downloader.py    HTTP 下载（断点续传、重试退避）
├── naming.py        文件名清洗、直链解析、文件名分配
├── manifest.py      身份清单（以 stream id 为键）
└── pipeline.py      主流程编排（采集池 + 解析池 + 下载池）
```

## 主流程

```
多个列表网址
     │
     ▼
并行采集（N 个浏览器实例，每个列表翻到底）
     │
     ▼
按 data-id 跨列表去重          ← 一个浏览器都不用开
     │
     ▼
扫保存目录，读每个 MP4 内嵌的 id  → 得到「已有什么」
     │
     ▼
分流：id 已存在 → 跳过
      id 不在   → 解析直链 → 下载 → 写入 id + 直链
```
