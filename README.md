# PrtScOCR —— 给 Ubuntu 的截图工具装上 OCR 按钮

> macOS 有 Live Text，Win11 截图工具（Snipping Tool）有"文本操作"。
> Ubuntu 的截图 UI 一直只有"复制/保存"。这个项目补上那一块：
> **按 PrtSc 框选，点一下「提取文字」，文字就进剪贴板。**

识别全部在本机完成，截图不出网卡，断网可用。

做法是**在 GNOME 原生截图 UI 上直接加一个按钮**——靠一个 GNOME Shell 扩展实现。

> 曾经走错过一次：一开始以为 gnome-shell 的内部 UI 碰不得，于是把 `Print` 从原生
> 截图工具上抢过来，自己画了一个覆盖层。那样确实能用，但**把录屏一起弄丢了**——
> 那是替换，不是增加。现在改回来了：`Print` 归还原生，按钮加在原生 UI 里。
> §4.4 记录了完整的排查与实现过程。

![界面](docs/overlay.png)

---

## 1. 它长什么样

按 **PrtSc** 打开的就是 **GNOME 原生截图工具**——不是我们做的仿制品。工具栏上多了
第四个按钮（下面是真机截图，不是效果图；`选区` 高亮是原生行为）：

![原生截图工具里的「提取文字」按钮](docs/native-ui-toolbar.png)

从左到右：`选区` / `屏幕` / `窗口` 是原生的三个模式，`提取文字` 是本项目加的第四个。
下面一排（相机 / 录屏 / 快门）也一个没动。

用法有两条，随便哪条都行：

- **先点按钮**：点「提取文字」→ 按钮变成 `请框选…`，屏幕上那个占位选区（边框 +
  四角把手 + 四周压暗的遮罩）会一起消失，给你一张干净的屏幕 → 拖拽框选 →
  **松手立刻识别**（这就是它的独立框选，不依赖上面那三个原生模式按钮）。
  一开始拖，框和遮罩就回来做实时反馈；再点一次按钮 = 取消并还原。
- **先框选**：直接拖拽框选 → 点「提取文字」→ 立刻识别

识别成功：文字进剪贴板，弹一条通知，界面自动关闭（和原生快门按钮点完就关一致）。
等待框选时再点一次按钮 = 取消。

> 为什么先点按钮不直接识别"打开时就有的那个框"？原生 UI 一打开就会在屏幕正中央
> 放一个约占 1/4 屏的默认选区，那只是个占位框，直接拿去 OCR 几乎肯定不是你要的，
> 所以必须先让你自己框一次。

原生的一切都没动：

| 原生功能 | 状态 |
| --- | --- |
| 区域 / 屏幕 / 窗口 三种模式 | 原样，按钮就在旁边 |
| **录屏**（底部「录制屏幕」切换） | **原样保留** |
| 显示指针、快门、保存到 `~/图片` | 原样 |
| `Print` / `Shift+Print` / `Alt+Print` | 原样（没有被抢） |

识别完成后：① 文字写入剪贴板；② 弹通知（带前 60 字预览）。

### 三个入口

| 入口 | 作用 | 依赖 |
| --- | --- | --- |
| **PrtSc** → 框选 → **提取文字** | 主入口：在原生截图工具里直接 OCR | Shell 扩展（§2.2） |
| **Super+Shift+Y** | 识别**剪贴板里已有的图片**并替换成文字——图从哪来都行（`Shift+Print` 区域截图、`Alt+Print` 窗口截图、浏览器右键复制、别人发的图…） | `--with-clip-hotkey` |
| **Super+Shift+S** | 备用的独立覆盖层截图工具（仿 Win11 那版，含结果面板可校对/合并单行） | 默认就装 |

> `Super+Shift+S` 这条是那次走弯路留下的产物。它**不和任何原生键冲突**，留着当兜底
> （比如扩展被 GNOME 升级弄坏时还能用）。不想要的话：`python3 lib/keys.py remove`
> 只删快捷键，或者 `python3 lib/keys.py install --no-…` 见 §3。

`Super+Shift+Y` 这条和原生截图配合起来特别顺：`Alt+Print` 截窗口 → `Super+Shift+Y`
→ 文字。原生负责各种截法，我们负责把图变成字。

## 2. 安装

```bash
git clone https://github.com/PeppaPig-114514/WaylandOCR.git ~/PrtScOCR
cd ~/PrtScOCR
./install.sh --with-extension --with-clip-hotkey   # 全程不需要 sudo
```

> 仓库叫 **WaylandOCR**，但安装路径一路用 `~/PrtScOCR`——所以 clone 的时候显式指定
> 目标目录（如上）。不指定的话会得到 `~/WaylandOCR`，`bin/ocr` 等的相对路径仍然能跑，
> 扩展也会自动认出这个位置（见 §2.2），但本文档其余部分都按 `~/PrtScOCR` 写。

安装脚本做的事：装一个隔离的 Python 环境（uv + rapidocr + onnxruntime）、
注册 systemd 用户服务（登录自启、崩溃自动重拉）、写应用入口、装 GNOME Shell 扩展
（在原生的截图 UI 上加「提取文字」按钮）、注册剪贴板识别快捷键。

> **原生的截图键一个都不动。** `Print` / `Shift+Print` / `Alt+Print` 全部保持
> GNOME 出厂设置，因为我们不再需要抢它们的键——按钮已经加进原生 UI 里了。
> （`install.sh` 早期版本会解绑 `Print`；现在重跑会自动把它恢复回来。）

可选参数：

```bash
./install.sh --with-extension        # 装「提取文字」按钮扩展（§2.2）
./install.sh --with-clip-hotkey      # 绑定 Super+Shift+Y = 识别剪贴板里的图片
./install.sh --no-keybinding         # 只装服务，不注册任何快捷键
./install.sh --binding '<Super>o'    # 换掉备用覆盖层的键（默认 <Super><Shift>s）
./install.sh --take-print            # 明确要求占用 Print（会顶掉原生 UI 和录屏，不推荐）
```

### 2.1 本机现状

| 键 | 行为 |
| --- | --- |
| `Print` | GNOME 原生截图 UI（区域/窗口/整屏/录屏），**多了个「提取文字」按钮** |
| `Shift+Print` | GNOME 原生区域截图 |
| `Alt+Print` | GNOME 原生窗口截图 |
| `Super+Shift+Y` | 识别剪贴板里的图片 → 文字 |
| `Super+Shift+S` | 备用独立覆盖层截图工具 |

### 2.2 那个「提取文字」按钮（Shell 扩展）

扩展在 `extension/prtsc-ocr@peppapig/`，装到
`~/.local/share/gnome-shell/extensions/`（软链接，所以改源码不用重装）：

```bash
./bin/install-extension              # 安装 + 启用
./bin/install-extension status       # 看状态
./bin/install-extension --remove     # 卸载
```

**装完必须注销重登一次**（Wayland 下没法只重启 gnome-shell）。原因：GNOME 只在
启动时扫描扩展目录，`lookup()` 读的是一张启动时就建好的表，运行时新增的 UUID
它不认识——实测 `gnome-extensions enable` 会直接回"扩展不存在"。

重登后验证：

```bash
gnome-extensions info prtsc-ocr@peppapig      # 状态应为 ACTIVE
journalctl --user -b | grep '\[prtsc-ocr\]'   # 应有两行：按钮已加入 / 已启用
```

回滚：

```bash
./uninstall.sh            # 停服务、删入口、卸扩展
./uninstall.sh --purge    # 连虚拟环境和日志一起删
```

卸载后系统里不留东西（全在用户目录，没碰过 /usr）。确认不需要了直接
`rm -rf ~/PrtScOCR`。

**OCR 后端是可配置的。** 扩展本身只是个 UI，识别交给外部程序做。默认路径是
`~/PrtScOCR/bin/ocr`，装到别处的话在这里改：

```bash
gnome-extensions prefs prtsc-ocr@peppapig          # 图形界面里改
# 或者直接改 dconf
gsettings set org.gnome.shell.extensions.prtsc-ocr ocr-command '/你的/路径/ocr'
gsettings reset org.gnome.shell.extensions.prtsc-ocr ocr-command   # 恢复默认
```

留空时会自动在 `~/PrtScOCR/bin/ocr` 和 `~/WaylandOCR/bin/ocr` 里挑第一个存在的
（照顾到仓库名和安装目录名不一致的情况）。

被调用的程序要接受 `--quiet <图片路径>`，把文字打到标准输出；退出码 `0` 成功、
`2` 表示图里没字、其它算失败（stderr 最后一行会显示在按钮上）。
`bin/install-extension` 会顺手把 `schemas/` 编译好——软链接安装不像
`gnome-extensions install` 那样自动编译，少了这步首选项读不到。

**想把它提交到 extensions.gnome.org：**

```bash
./bin/build-extension            # 校验 + 打包到 dist/
./bin/build-extension --check    # 只校验
```

打包用的是官方 `gnome-extensions pack`，产物是能直接上传到那个站点的 zip。
详见 §11。

## 3. 用法

```bash
# 唤起备用覆盖层截图工具（Super+Shift+S 绑的就是它）
~/PrtScOCR/bin/prtsc-ocr
~/PrtScOCR/bin/prtsc-ocr --image 图.png     # 不抓屏，直接对已有图片框选

# 「提取文字」按钮是 Shell 扩展的一部分，不在这个 CLI 里；
# 它的安装/状态/卸载：~/PrtScOCR/bin/install-extension [status|--remove]

# 识别剪贴板里的图片（Super+Shift+Y 绑的就是它）
~/PrtScOCR/bin/ocr-clip
~/PrtScOCR/bin/ocr-clip --dry-run                        # 只看结果，不动剪贴板

# 命令行识别图片（`--no-autostart` 时服务没起就报错，不偷偷拉起）
~/PrtScOCR/bin/ocr 图片.png
~/PrtScOCR/bin/ocr 图片.png --region 100,200,400,80      # 只识别该区域
~/PrtScOCR/bin/ocr 图片.png --json                       # 带坐标/置信度
~/PrtScOCR/bin/ocr 图片.png --copy                       # 顺便进剪贴板
~/PrtScOCR/bin/ocr 截图/*.png --quiet                    # 批量

# 服务管理
~/PrtScOCR/bin/ocrd status | restart | log | run | warm
```

`bin/ocr` 的退出码：`0` 成功、`1` 失败、`2` 没识别到文字、`3` 服务不可用——方便脚本里判断。

另外，任何程序都不必知道本项目的存在：`bin/ocr` 读的是普通图片文件，
所以文件管理器右键、`xargs`、编辑器插件之类的接法都很直接。

## 4. 设计取舍（为什么是这么做的）

### 4.1 为什么不直接用现成的

Linux 上做截图 OCR 的前人经验不少，都看过了：

| 项目 | 形态 | 为什么没有直接用 |
| --- | --- | --- |
| [Native Screenshot UI OCR Extended](https://extensions.gnome.org/extension/10254/native-screenshot-ui-ocr-extended/) | **GNOME 扩展，直接在原生截图 UI 里加按钮 → Tesseract** | **最终形态和本项目最像的一个，见 §4.1.1。差别不在 UI 而在引擎** |
| [NormCap](https://github.com/dynobo/normcap) | Python + Qt，框选→Tesseract | 引擎是 Tesseract，中文精度明显不如 PP-OCR；且每次都要冷启动一个进程 + 引擎 |
| [TextSnatcher](https://github.com/RajSolai/TextSnatcher) | GTK/Vala + Tesseract | 同上；项目久未维护 |
| [gocr（GNOME 扩展）](https://github.com/EvilX/gocr) | 扩展自绘框选覆盖层 → Tesseract | 画的是自己的覆盖层，没进原生 UI；引擎同样是 Tesseract |
| Frog / gImageReader | 打开图片再识别 | 不是"截图即识别"的顺手流程 |

结论：**流程照抄前人（框选 → 识别 → 剪贴板），引擎换掉**，并且把引擎做成常驻服务。

#### 4.1.1 和 EGO #10254 的关系

先说清楚：**"在原生截图 UI 上加一个 OCR 按钮"这件事，本项目不是第一个做的。**

[oreocapybara/gnome-native-screenshot-ui-ocr-extended](https://github.com/oreocapybara/gnome-native-screenshot-ui-ocr-extended)
（EGO [#10254](https://extensions.gnome.org/extension/10254/native-screenshot-ui-ocr-extended/)，
2026-06 发布）早就做了，而且是**独立想到同一个办法**：GNOME 没给扩展留任何插件点，
`org.gnome.Shell.Screenshot` 的 D-Bus 接口又被 `_senderChecker` 白名单挡着，
所以两家都只能往 `ScreenshotUI._typeButtonContainer` 里塞第四个按钮——连
`style_class: 'screenshot-ui-type-button'`、`x_expand: true` 这些细节都撞车了，
因为那是在当前 Shell 里唯一能做出"看起来像原生"的写法。

**所以本项目在 UI 层面没有新颖性**，这点不装。真正不一样的是按钮按下去之后：

| | #10254 | 本项目 |
| --- | --- | --- |
| 引擎 | Tesseract | RapidOCR（PP-OCR，ONNX Runtime） |
| 识别语言 | 调用时**不带 `-l`** → Tesseract 默认 `eng`；schema 里只有快捷键 / 面板图标 / 通知 / 按钮位置四个 key，**没有语言选项** | 中文优先，`tests/` + `bench/` 有量化结果（1419 字错 3 个） |
| 安装 | 要 `sudo apt install tesseract-ocr`（中文还得加 `tesseract-ocr-chi-sim`，而且加了也选不上） | **全程零 sudo**，uv 装进 `~/.venv` |
| 换引擎 | 不支持 | 首选项里可填任意 `<程序> --quiet <图>` |
| 换行 | 单个 `\n` 合并成空格（为正文写的） | 原样保留（代码 / 终端 / 表格不会被拍平） |
| 触发 | 点按钮 → 拖选 → **再点原生快门** | 拖选 → **松手即识别** |
| 原生按钮 | OCR 期间禁用屏幕 / 窗口 / 录屏 / 指针 | 一个都不动 |
| 覆盖版本 | Shell 45–50 | 仅 50 |
| 其他 | 面板图标、可配快捷键、CI / eslint / 测试 | 无 |

一句话：**UI 撞车，引擎分野**。#10254 调用 `tesseract <图> stdout` 时不带 `-l`，
Tesseract 默认就是英文模型，**中文场景基本不可用**——这是本项目存在的理由。
反过来，它在 GNOME 45–50 上都能装、有面板入口和 CI，这些是本项目暂时比不上的。

### 4.2 引擎：RapidOCR（PP-OCRv6，ONNX Runtime）

* 中英混排精度远好于 Tesseract，模型随包分发、**不联网**；
* 纯 pip 安装，**不需要 sudo**，不碰系统包；
* 装在 `~/PrtScOCR/.venv`（uv 拉取的独立 Python 3.12），跟系统 Python 完全隔离。

### 4.3 后台常驻，随叫随到

`lib/ocrd.py` 是一个 UNIX socket 服务（`$XDG_RUNTIME_DIR/prtsc-ocr.sock`），
由 systemd 用户服务托管（`Restart=always`，杀掉 4 秒内自动回来）。

关键点是**把冷启动成本挪到启动时**：onnxruntime 首次遇到某个输入形状要分配内存
并做图优化，不预热的话登录后第一次识别要多花 4 秒以上。所以服务启动时会用
1280×720 / 640×360 两档真实尺寸各跑一遍，`ping` 返回的 `warm` 字段表示预热完成——
安装脚本和自动拉起逻辑等的都是 `warm` 而不是"模型加载完"。

### 4.4 两种截图链路，以及为什么最后选了扩展

**方案 A：自己抓屏 + 自绘覆盖层**（`Super+Shift+S` 走的就是这条）。

| 方案 | 结果 |
| --- | --- |
| `maim` / `scrot` / `import` | X11 专用，Wayland 下不可用 |
| `grim` / `slurp` | 只支持 wlroots 合成器，GNOME 用不了 |
| `org.gnome.Shell.Screenshot` (D-Bus) | 本机实测 **AccessDenied: Screenshot is not allowed**。源码里 `_senderChecker` 只放行 `org.gnome.SettingsDaemon.MediaKeys` 和 `org.freedesktop.impl.portal.desktop.gnome` 两个调用者 |
| `xdg-desktop-portal` Screenshot | ✅ `interactive=false` 在 GNOME 50 上**静默**返回整屏 PNG，约 0.5s，无授权弹窗 |

流程：先静默抓一张整屏 → 用自己的全屏窗口把这张**冻结帧**画出来 → 用户在冻结帧上
框选 → 只把框选的那块像素送去 OCR。好处是截图时不会拍到工具自身。抓到的临时文件
读进内存后**立即删除**（`PRTSC_OCR_KEEP_IMAGE=1` 可保留）。

代价是：**这是一个仿制品**，不是原生 UI。GNOME 的录屏、窗口截图我们都没有，
只能靠抢 `Print` 键来顶替原生工具——那等于让用户丢功能。所以又走了方案 B。

**方案 B：GNOME Shell 扩展，往原生 UI 上加按钮**（主入口，`Print` 走的就是这条）。

GNOME 原生截图工具的实现在 gnome-shell 里，源码是
`/org/gnome/shell/ui/screenshot.js`（GNOME 50 共 3142 行，编译在
`/usr/lib/gnome-shell/libshell-18.so` 的 gresource 里，`gresource extract` 能取出来）。
它没有配置项、没有插件点、D-Bus 也只对两个白名单调用者开放
（`InteractiveScreenshot` 同样要过 `_senderChecker`），**但它是可以改的**——
GNOME Shell 扩展和 gnome-shell 跑在同一个进程里，能直接动它的 UI 对象树。

关键结构（行号对应 GNOME 50）：

| 符号 | 行 | 用途 |
| --- | --- | --- |
| `export const ScreenshotUI` | 1078 | 整个截图 UI 的类，**已导出**，扩展能 import |
| `this._typeButtonContainer` | 1265 | 装「区域/屏幕/窗口」三个按钮的容器 |
| `IconLabelButton` | 53 | 那三个按钮的类（未导出，按同结构复制） |
| `screenshot-ui-type-button` | — | 它们的样式类（在 Yaru 主题的 css 里） |
| `this._stageScreenshot` | 1173 | 冻结帧，取像素的来源 |
| `UIAreaSelector.getGeometry()` | 334 | 选区 → `[x,y,w,h]`（**不是** `_result`） |
| `UIAreaSelector` 的 `drag-ended` 信号 | 254 | 松手时机，用来做"框完就识别" |
| `ScreenshotUI._getSelectedGeometry()` | 1871 | 选区 → `[x,y,w,h]` |
| `Shell.Screenshot.composite_to_stream()` | 2416 | 真正的取像素调用 |

所以扩展只做四件事：`import {ScreenshotUI}`；包装它的 `open()`，在 UI 打开后往
`_typeButtonContainer` 里 `add_child` 一个自己的按钮；点按钮时照抄原生"保存截图"的
取像素路径从**冻结帧**（不是重新截屏，否则会把遮罩和面板一起拍进去）裁出选区；
把 PNG 交给常驻 OCR 服务，文字进剪贴板，关界面。

**踩过的四个坑**（前两个是交互 bug，后两个是 API 用法）：

1. **选区坐标要用 `UIAreaSelector.getGeometry()`，不是 `_result`。**
   `_result` 属于另一个类 `SelectArea`（D-Bus `SelectArea` 那套），
   `UIAreaSelector` 上根本没有这个字段。实测：`'_result' in _areaSelector` → `false`。
   用错的后果是**每点必失败**，而且还会误报"请先框选"——用户明明框了也不认。
2. **截图 UI 打开时 `Main.notify` 弹不出来。** 那是模态抓取状态，通知要等 UI 关掉
   才出现，表现得像"点了没反应，退出后才蹦提示"。所以等待/失败这类即时反馈必须写
   在按钮标签上（`提取文字` → `请框选…` / `识别中…` / `没识别到文字`），
   通知只当留档。
3. **`Gio.Subprocess.communicate_utf8_async` 没有被 promisify，不能直接 `await`。**
   直接 await 会报 `At least 3 arguments required, but only 2 passed`——它要 callback。
   gnome-shell 只 promisify 了 screenshot.js 自己用的那几个
   （`composite_to_stream`、`screenshot_stage_to_content` 等，见该文件 19-24 行），
   所以这里要自己 `Gio._promisify(Gio.Subprocess.prototype, 'communicate_utf8_async')`。
   好消息：`_promisify` 是幂等的，重复调用不会炸。
4. **promisify 之后返回的是 `[stdout, stderr]` 两项，不是三项。**
   它和 `communicate_utf8_finish()` 不一样——开头那个 `success` 布尔被 promisify
   吃掉了（失败会直接 reject）。写成 `const [, stdout, stderr] = ...` 会把 stderr
   当成识别结果：**识别永远返回空，且错误信息全被吞掉**，只看到"没识别到文字"。

还有一条不是坑但容易漏：`bin/ocr` 的退出码 **2 表示"图没问题、只是没字"**，
不是失败。当成失败处理的话，框到空白处会弹"提取文字失败"，而不是友好的
"没识别到文字"。

**"把框藏起来"这条也依赖内部结构：** 那个框由 `UIAreaIndicator`（4 块
`screenshot-ui-area-indicator-shade` 压暗遮罩 + 1 块 `...-selection` 边框）
加 4 个 `screenshot-ui-area-selector-handle` 把手组成。整个 `UIAreaSelector`
里**没有任何一处** `show()`/`hide()`，也就是原生从不改它们的可见性——所以藏了
就得自己负责还：`drag-started` 时放回来（拖动要看得到），取消 /
再次打开 UI / `disable()` 时都要还原，否则会把用户的原生界面留在"框没了"的状态。
只藏边框不藏遮罩是没用的：中间那块亮的矩形本身还是个框。

必须包装 `open()` 而不是 `_init()`：`Main.screenshotUI` 这个单例在 shell 启动时就
`new` 好了（`main.js:260`），那时 `_init` 早跑完了，扩展来不及插手。

扩展依赖的全是私有字段，GNOME 换版本可能改名——所以每处访问都做了存在性判断，
真改名了按钮会提示而不是把 shell 弄崩。这是所有 GNOME 扩展的通病。

**实测验证**（用一个临时的无头 shell 跑的，不干扰桌面会话）：

```
gnome-shell --headless        # 独立 dbus 会话，无窗口
  → gnome-extensions info prtsc-ocr@peppapig
    状态: ACTIVE

  → _typeButtonContainer 子元素数=4
      子[0] screenshot-ui-type-button 标签=选区
      子[1] screenshot-ui-type-button 标签=屏幕
      子[2] screenshot-ui-type-button 标签=窗口
      子[3] screenshot-ui-type-button 标签=提取文字   ← 加进去的
    Shell.Screenshot.composite_to_stream: function
```

原生三个按钮都在（没被挤掉），我们的是第四个、样式类相同——所以长得一样。

方案 A 的覆盖层保留下来当兜底（`Super+Shift+S`），它不和任何原生键冲突。

### 4.5 快捷键：只能走 gsettings

Wayland 下普通程序不能监听全局键盘（X11 的 `XGrabKey` 被彻底移除），所以自定义
快捷键只能由合成器转发。本项目用 GNOME 的 **`media-keys custom-keybindings`**：

| 键 | 命令 | 说明 |
| --- | --- | --- |
| `<Super><Shift>s` | `bin/prtsc-ocr` | 备用覆盖层 |
| `<Super><Shift>y` | `bin/ocr-clip` | 剪贴板图片识别（可选） |

**`show-screenshot-ui` 不动。** 早期版本会把它清空好独占 `Print`，那会让用户丢掉
录屏；现在 `Print` 归原生，主入口的按钮由扩展提供（§4.4）。`lib/keys.py install`
默认还会检查这个键有没有被清空过，被清空就顺手恢复回来。

改键：`lib/keys.py install --binding '<Super>o'`，或者 GNOME 设置里的"自定义快捷键"。
`./uninstall.sh` 会把这些条目清掉；`state/gnome-keys-backup.json` 存着历史原值。

## 5. 配置

全部通过环境变量（systemd 单元里改 `Environment=` 后 `bin/ocrd restart`）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PRTSC_OCR_LIMIT_TYPE` | `max` | 检测模型缩放策略。**别改回 `min`**——库默认的 `min/736` 会把小图短边放大到 736（420×120 直接变 2576×736），实测精度没有任何提升，耗时却是 25 倍 |
| `PRTSC_OCR_LIMIT_SIDE` | `960` | 检测输入的最长边上限。实测 960 与 1280 逐图结果完全相同，小图上 960 还更快，所以没有理由用 1280 |
| `PRTSC_OCR_DET_MODEL` | `small` | 可选 `tiny` / `small` / `medium` / `mobile` / `server`（精度↑ 速度↓，模型已随包内置） |
| `PRTSC_OCR_REC_MODEL` | `small` | 同上 |
| `PRTSC_OCR_OCR_VERSION` | `PP-OCRv6` | 可选 `PP-OCRv5`（v5 要配 `mobile`）|
| `PRTSC_OCR_BOX_THRESH` / `PRTSC_OCR_THRESH` | `0.45` / `0.25` | 检测框阈值，漏检多就调低 |
| `PRTSC_OCR_PANEL` | `1` | 识别后是否弹结果面板（`0` = 纯静默进剪贴板） |
| `PRTSC_OCR_KEEP_CAPTURE` | `0` | 是否保留 portal 落盘的原始截图 |
| `PRTSC_OCR_SOCK` | 见上 | socket 路径 |

> 模型名大小写不敏感，写枚举名或枚举值都行（`TINY` 与 `tiny` 等价，`PPOCRV6` 与 `PP-OCRv6` 等价）。
>
> **要不要换 `tiny`？** `tiny` 快一倍以上（本机实测小区域 106ms → 约 50ms），纯中文正文、
> 中文+数字、低对比度灰字、两栏段落上和 `small` **测不出差别**；但它在三类内容上会系统性出错
> （`bench/RESULTS.md` §5.3 有实例）：
> ① ASCII 大小写与 `O`/`0` 混淆（`RapidOCR`→`RapidocR`、`Ctrl+Shift+O`→`Ctrl+Shift+0`）；
> ② 全角标点被折成半角（`！？`→`!?`、`（）`→`)`）；
> ③ 代码标点间距（`def main():`→`def main() :`）。
> 所以**经常框选代码、命令行、序列号、快捷键的话，保持 `small`**。

改完要重启服务：

```bash
# 方式一：临时试
PRTSC_OCR_DET_MODEL=tiny PRTSC_OCR_REC_MODEL=tiny bin/ocrd run --foreground

# 方式二：写进 systemd 单元，长期生效
systemctl --user edit prtsc-ocr.service     # 加一行 Environment=
bin/ocrd restart
```

## 6. 性能与精度实测

### 6.1 精度（与机器负载无关，数据可靠）

用 `bench/bench.py` 测的：12 张样本、1419 个非空白字符，覆盖 12px 小字、密集正文、
低对比度、深色终端、两栏排版、全角符号、1080p 整屏。完整报告见
**[bench/RESULTS.md](bench/RESULTS.md)**。

**CER = 字符错误率。**主口径的 CER 会被中英文之间的空格差异主导（35~47 处编辑来自
空格，而真正的字符错误只有 0~8 处），所以下面按"真正的字符错误"排序——
对"把文字丢进剪贴板"这个用途，这才是该看的数。

| 配置 | 非空白字符错误 | 忽略空格后 CER | 主口径 CER |
| --- | --- | --- | --- |
| v6 medium | 1 / 1419 | 0.002 | 0.027 |
| 库默认 `min/736`（small） | 3 / 1419 | 0.004 | 0.027 |
| **本机默认：`max/960`（small）** | **3 / 1419** | **0.004** | 0.031 |
| tiny 检测 + small 识别，`max/960` | 3 / 1419 | 0.004 | 0.032 |
| v5 mobile，`max/960` | 6 / 1419 | 0.007 | 0.038 |
| v6 tiny，`max/960` | 8 / 1419 | 0.011 | 0.040 |

三条结论：

1. **库默认的 `limit_type=min` 是纯亏**。它把 420×120 的框选放大成约 2576×736
   （短边拉到 736），小区域要 1.9~2.1 秒，而字符错误数和 `small/max` 一模一样
   （都是 3/1419）。机制上，`min` 是"整页扫描文档、怕小字看不清"的假设，
   对"本来就清晰的屏幕截图"只是白烧算力。改成 `max` 是本项目最值钱的一行配置。
2. **不要在识别前放大图片**。曾以为"小图放大能救小字"，实测（`RESULTS.md` §4.8
   + 本机复验）在 12px 样本上识别结果**逐字相同**、延迟却翻 2~3 倍
   （420×120 区域 250ms → 91ms）。所以 `lib/ocrd.py` 里那个"小于 480px 就 2× 放大"
   的启发式已经删掉了。
3. **`limit_side_len` 960 与 1280 逐图结果完全相同**，没有理由用 1280。

### 6.2 延迟（空闲机器实测，当前默认配置）

9 次取中位，机器上只有本项目自己的常驻服务：

| 框选内容 | 中位 | P95 | 最快 |
| --- | --- | --- | --- |
| 小区域 12px 小字（420×120） | **106 ms** | 146 ms | 93 ms |
| 典型一行文字（680×130） | **200 ms** | 250 ms | 169 ms |
| 一段正文（900×420） | 230 ms | 350 ms | 214 ms |
| 设置弹窗（560×300） | 345 ms | 427 ms | 282 ms |
| 整个 1080p 屏幕（最坏情况） | 2498 ms | 2908 ms | 2313 ms |

> **注意别被基准报告里的绝对耗时误导**：`bench/bench.py` 一跑起来 onnxruntime 就会
> 用满 6 个物理核，加上本项目自己的常驻服务常占约 1 个核，报告里的延迟是**上界**
> （同一配置两轮复测能差 20%+）。上表是把机器空出来之后重新测的。
> 精度（CER）不受负载影响，所以 §6.1 的表可以直接信。

固定开销：抓屏（portal 静默）约 500 ms。

常驻内存：正常框选约 160–200 MiB；做过整屏这种大图之后会涨到约 460 MiB，然后
**就稳定在那儿**。这不是泄漏——rapidocr 默认已经把 onnxruntime 的 CPU 内存池关了
（`enable_cpu_mem_arena: false`），涨上去的是分配器把释放掉的大块内存留在进程里备用、
不还给操作系统。实测在这个水位上再打 33 次各种尺寸的识别，只增长 9 MiB。

**端到端：按下 PrtSc → 框选 → 点「提取文字」→ 文字进剪贴板，约 0.7 秒**，
其中 OCR 本身只占约 0.2 秒。

## 7. 自检与排错

```bash
# 一组端到端断言（在私有 socket 上跑，不影响正在用的服务）
~/PrtScOCR/.venv/bin/python ~/PrtScOCR/tests/smoke_test.py

# 服务状态 / 日志
~/PrtScOCR/bin/ocrd status
~/PrtScOCR/bin/ocrd log
```

| 症状 | 处理 |
| --- | --- |
| **按 Print 有原生截图工具，但没有「提取文字」按钮** | 扩展没加载。先 `gnome-extensions info prtsc-ocr@peppapig`：<br>· 报"扩展不存在"或状态不是 ACTIVE → **注销重登一次**（GNOME 只在启动时扫扩展目录，§2.2）<br>· 状态是 ERROR → `journalctl --user -b \| grep prtsc-ocr` 看报错，多半是 GNOME 升级改了内部结构（§4.4）<br>· 被自动禁用 → `gnome-extensions enable prtsc-ocr@peppapig` |
| 按钮点了没反应 | 看按钮文字：变成 `请框选…` 就是**在等你拖拽框选**（松手即识别），不是卡住了；再点一次可取消 |
| 按钮显示 `还没框选` | 没拖到有效区域，或拖得太小（宽高需 > 1 像素） |
| 按钮在，显示「没拿到屏幕冻结帧」 | 抓屏失败，不是 OCR 的问题 |
| 按 Print 没反应 | `python3 lib/keys.py status` 看原生键有没有被改掉；正常应为 `show-screenshot-ui = ['Print']`，不对就重跑 `./install.sh` |
| 按 Print 弹出的是**我们**的覆盖层而不是原生 UI | `show-screenshot-ui` 被解绑过（早期版本会这样）。重跑 `./install.sh` 会自动恢复；或 `gsettings set org.gnome.shell.keybindings show-screenshot-ui \"[\'Print\']\"` |
| **提示"portal 截图被拒绝（response=2）"** | 见下方 §7.1，这是 Ubuntu portal 的授权机制，不是抓屏坏了 |
| 抓屏失败 / 超时 | 确认是 Wayland 会话（`echo $XDG_SESSION_TYPE`）；portal 服务异常时 `systemctl --user restart xdg-desktop-portal-gnome` |
| 识别不准 | 区域框小一点、避开半透明/艺术字；**框选时把整行文字完整框进去**——如果框线正好从字中间切过，被切掉的下半截会让"天"认成"于"（实测过）。宁可多框一点边距 |
| 想更准/更快 | §5 换模型（`medium` 更准、`tiny` 更快），或调 `PRTSC_OCR_BOX_THRESH` |
| 第一次识别很慢 | 预热还没跑完（看 `ocrd status` 的 `warm`）；或手动 `bin/ocrd warm` |
| 剪贴板粘贴不出图 | 依赖 `wl-clipboard`（`sudo apt install wl-clipboard`） |

### 7.1 为什么备用覆盖层会报 `response=2`

> **这一节只对 §4.4 的方案 A（`Super+Shift+S` 覆盖层）适用。** 主入口那个「提取文字」
> 按钮走的是 Shell 扩展，取像素用的是 gnome-shell 内部接口，**完全不经过 portal**，
> 所以不会有这个问题。

**症状**：快捷键按下后弹通知"抓屏失败：portal 截图被拒绝（response=2）"，但从终端手动跑
`bin/prtsc-ocr` 却一切正常。日志里能同时看到这两行：

```
systemd[2950]: Started app-gnome-prtsc\x2docr-166076.scope - Application launched by gsd-media-keys.
xdg-desktop-portal[3753]: Failed to show access dialog: AccessDenied:
  Only the focused app is allowed to show a system access dialog
```

**原因**：portal 允许谁截图是按调用者的 **app-id** 判定的，而 app-id 由进程的 **cgroup**
推出来。GNOME 的快捷键由 `gsd-media-keys` 处理，它是通过 systemd 的"应用 scope"拉起命令的
——于是进程落在 `app-gnome-prtsc-ocr-<pid>.scope` 里，portal 认出一个 app-id
`gnome-prtsc-ocr`。这个 app-id 从没被授权过，portal 就要弹一个"允许截图吗"的对话框；
可那个对话框**只能由前台应用弹出**，而本工具是"先抓屏、后建窗"，请求发出时还没有窗口
→ 不是前台 → 对话框弹不出来 → 直接拒绝，`response=2`。

从终端跑之所以没事：那时候进程不在 `app-*.service/scope` 里，portal 认不出具体应用，
按 **host（app-id 为空）** 处理，而权限库里 host 是已授权的
（`gnome-screenshot` 这类系统工具走的就是这条路）。

**处理**：`bin/prtsc-ocr` 会先把自己塞进一个**中性命名的 transient scope**
（`run-*.scope`）再跑，app-id 就回到空值、命中已有授权。不需要点任何"允许"，
也不用改系统权限库。两个相关开关：

```bash
PRTSC_OCR_NO_SCOPE=1 ~/PrtScOCR/bin/prtsc-ocr   # 关掉这层包装，用于对比验证
```

判断到底是哪种情况，看 `~/PrtScOCR/logs/snipper.log`——每次启动都会记一行自己的
cgroup 和包装状态（快捷键拉起时 stderr 只进 journal，很容易漏掉）：

```
2026-09-29 09:14:55 启动 pid=168147 args=[...] cgroup=.../run-p168147-i174798.scope　中性 scope 包装=是
```

- `cgroup` 是 `run-*.scope`、包装=`是` → 正常，走的是 host 授权。
- `cgroup` 是 `app-*.service`/`app-*.scope`、包装=`否` → 说明它没经过 `bin/prtsc-ocr`
  （例如有人直接调 `lib/snipper.py`），改回用 `bin/prtsc-ocr` 即可。

想自己核对权限库（`flatpak` 未安装时）：

```bash
gdbus call --session --dest org.freedesktop.impl.portal.PermissionStore \
  --object-path /org/freedesktop/impl/portal/PermissionStore \
  --method org.freedesktop.impl.portal.PermissionStore.Lookup screenshot screenshot
# → ({'': ['yes']}, <byte 0x00>)   # 空 app-id（host）已授权
```

## 8. 已知限制

* **只在 GNOME Wayland 上验证过**（本机 GNOME 50 / Ubuntu 26.04）。KDE 下 portal
  与快捷键机制不同，需要另做适配（`lib/capture.py` 走的是标准 portal，理论上能复用）。
* **多显示器**：冻结帧尺寸取自 portal（当前为整屏并集），窗口按等比居中适配，
  坐标换算已经按 `scale/offset` 处理；但本机只有一个 1920×1080 显示器，
  多屏 + 分数缩放没有实测过。
* **单行模式有坑已绕过**：rapidocr 的 `RapidOCR.__call__` 会把 `use_det` 写进实例状态，
  一次 `use_det=False` 会污染之后所有调用（表现为后续识别变成一两个乱码字）。
  `lib/ocrd.py` 现在每次调用都显式传 `use_det` / `use_cls`，并用 `tests/smoke_test.py`
  把"单行识别之后普通识别仍然正常"固化成了回归用例。
* **版面重建是启发式的**：行分组阈值 0.6×行高、词间补空格阈值 0.55×行高，都是经验值；
  只对"规整的多栏/表单"有效。真实界面里的复杂排版（不规则卡片、图文混排）可能行序不对
  ——基准报告里两栏样本若用朴素的行优先顺序，CER 会从 0.011 飙到 0.77。
  单行文字本身是准的，"还原排版"只是尽力而为。
* **`tiny` 模型有三类系统性出错**（除非你换了模型，见 §5）：ASCII 大小写与 `O`/`0` 混淆、
  全角标点被折半角、代码标点间距。默认用 `small` 就是为了避开这些。
* 首屏是**冻结帧**（先截图再框选），不是 Win11 那种实时预览——这是 portal 方案的
  固有限制，换来的是不用装 GNOME 扩展、不依赖 shell 内部 API。
* **顶行放第四个按钮，可能触发 Shell 的布局 bug**：挂起或熄屏之后，那一行会被拉伸到
  满屏宽。这是 [#10254 的作者实测确认](https://github.com/oreocapybara/gnome-native-screenshot-ui-ocr-extended)
  的——`_typeButtonContainer` 是 homogeneous 容器，孩子从 3 个变成 4 个之后，
  suspend/blank 触发的重排会把容器和所有孩子都按同一个宽度铺满。
  **本机没有复现**（要真挂起才能触发），但既然人家为此专门改了默认布局，可信度不低。
  真遇到了：**关掉截图界面重开一次**就恢复。#10254 的规避办法是把按钮放到下面一行
  （图标模式，挨着「显示指针」）——如果你更在意稳定性而不是"和选区/屏幕/窗口并排"，
  可以照做（要改的是 `extension.js` 里 `makeTypeButton` 的挂载位置）。

## 9. 目录结构

```
PrtScOCR/
├── install.sh / uninstall.sh     一键安装 / 回滚（都无需 sudo）
├── extension/
│   └── prtsc-ocr@peppapig/        GNOME Shell 扩展：往原生截图 UI 加「提取文字」
│       ├── metadata.json          扩展元信息（含 GSettings schema 声明）
│       ├── extension.js           全部逻辑：加按钮、框选、取像素、调 OCR
│       ├── prefs.js               首选项界面（改 OCR 后端路径）
│       ├── LICENSE                GPL-3.0-or-later
│       └── schemas/               GSettings schema（gschemas.compiled 是编译产物）
├── bin/
│   ├── install-extension          安装/卸载那个扩展
│   ├── build-extension            打包成可上架 extensions.gnome.org 的 zip
│   ├── prtsc-ocr                  唤起备用覆盖层（Super+Shift+S 绑的就是它）
│   ├── ocr                       命令行识别
│   ├── ocr-clip                  识别剪贴板图片
│   └── ocrd                      服务管理
├── lib/
│   ├── snipper.py                GTK4 冻结帧 + 框选 + 工具栏（系统 python3）
│   ├── capture.py                portal 静默抓屏
│   ├── ocrd.py                   常驻 OCR 服务（venv python）
│   ├── ocr_client.py             socket 客户端（纯标准库）
│   └── keys.py                   GNOME 快捷键注册 / 回滚
├── config/                       systemd 单元与 .desktop 模板
├── tests/smoke_test.py           端到端自检（22 条断言，跑在私有 socket 上）
├── bench/                        精度-延迟基准
│   ├── bench.py                  合成 12 张测试图 + 全量评测（约 11 分钟）
│   ├── RESULTS.md                ← 基准报告，选型依据都在这里
│   └── summarize.py / analyze_errors.py   出表 / 错误分类
├── docs/overlay.png              界面截图
└── state/                        安装前的 GNOME 键位备份（用于回滚）
```

> `bench.py` 会自己合成测试图并写进 `bench/samples/`，Ground Truth 就是渲染时用的
> 字符串，不存在人工转录环节；所以整份报告可以从零复现。

## 10. 许可与来源

本项目代码原创，以 **GPL-3.0-or-later** 发布（见 `LICENSE`；与 gnome-shell 本身
同系，提交到 extensions.gnome.org 不会有许可问题）。
OCR 能力来自开源项目 [RapidOCR](https://github.com/RapidAI/RapidOCR)（Apache-2.0），
模型为 PaddleOCR 的 PP-OCR 系列（Apache-2.0）。流程设计上参考了上文表格里
NormCap / TextSnatcher / gocr 等前人工作，特此致谢。

## 11. 上架 extensions.gnome.org

仓库：<https://github.com/PeppaPig-114514/WaylandOCR>

### 打包

```bash
./bin/build-extension            # 校验 + 打包到 dist/
./bin/build-extension --check    # 只跑校验
```

产物是 `dist/prtsc-ocr@peppapig.shell-extension.zip`，用官方
`gnome-extensions pack` 生成，脚本会逐条检查上架要求：

| 检查项 | 为什么 |
| --- | --- |
| `metadata.json` 在 zip **根目录** | 站点按这个位置找元信息，多套一层目录会被拒 |
| `uuid` / `name` / `description` / `shell-version` 齐全 | 列表页要用 |
| `shell-version` 只写纯数字版本 | 写 `"50.1"` 或未来版本会被打回 |
| `url` 不能是占位符 | 列表页要显示主页 |
| schema 过 `--strict` 校验 | key 名笔误之类在这里就挡住 |
| 解包后 `gjs -m` 能解析 `extension.js` / `prefs.js` | 防止把坏文件传上去 |
| zip 里不带 `gschemas.compiled` | 那是编译产物，装的时候会重新生成 |

### 提交

1. https://extensions.gnome.org/upload/ ，用 GNOME 账号登录
2. 上传那个 zip，站点自己读 `metadata.json`
3. 进审核队列（通常几天到几周）；通过前只有你自己能装

### 审核员会在意什么

老实说，这个扩展用了 gnome-shell 的**私有内部结构**（`ScreenshotUI` 的
`_typeButtonContainer`、`_stageScreenshot`、`UIAreaSelector` 等，见 §4.4 的表）。
这不是"不小心"，而是**目前唯一能往原生截图 UI 上加按钮的途径**：GNOME 没有
提供任何插件点，D-Bus 也只放行 gsd-media-keys 和 portal 两个白名单调用者
（源码里的 `_senderChecker`）。

提交时值得在备注里主动说明这一点，并强调：

- 每一处私有字段访问都有存在性判断，结构变了会退化成"按钮给个提示"，**不会把
  shell 弄崩**；
- 原生功能一个没动，`Print` / `Shift+Print` / `Alt+Print` 都是出厂绑定（有回归
  测试守着，见 `tests/`）；
- 识别完全在本机，扩展本身不联网。

另外 `name` 和 `description` 目前是**中英双语**（列表页给英文访客看，同时保留
中文），但按钮上的文字是中文（`提取文字` / `请框选…`）。如果要在国际上更通用，
下一步是接 gettext——不过当前机器上没有 `msgfmt`，需要先装 `gettext` 包。

### 审核员会问"这和 #10254 有什么区别"

主动答（也写进提交备注）：**UI 思路是同一个，本项目的新意在引擎**——#10254 调
Tesseract 且不带 `-l`，默认英文模型，中文不可用；本项目自带 PP-OCR 中英模型、
零 sudo、后端可换，且有基准测试。完整对照见 §4.1.1。

### 别人装了怎么用

扩展只是个 UI，**识别要靠本机的 OCR 后端**。别人在扩展首选项里把「OCR 程序路径」
指到自己的后端即可；本项目 `README` 的 §2 就是那套后端的完整安装步骤。
