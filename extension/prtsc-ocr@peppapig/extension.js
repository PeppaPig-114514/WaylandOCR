/**
 * SPDX-License-Identifier: GPL-3.0-or-later
 * 截图 OCR —— 在 GNOME 原生截图 UI 上增加一个「提取文字」按钮。
 *
 * 为什么是扩展
 * ------------
 * GNOME 的截图工具（区域 / 窗口 / 整屏 / 录屏 / 显示指针）是 gnome-shell 自己的
 * `js/ui/screenshot.js`，不对外提供配置或插件点，D-Bus 也只放行两个白名单调用者
 * （源码里 `_senderChecker` 只允许 gsd-media-keys 和 portal）。
 * 想"在原生 UI 上加一个按钮"，官方唯一支持的途径就是 GNOME Shell 扩展：扩展和
 * gnome-shell 跑在同一个进程里，可以直接改它的 UI 对象树。
 *
 * 为什么这个做法比"另做一个截图工具"好
 * ------------------------------------
 * 我们自己再做一个覆盖层，就等于把原生那套（尤其是录屏）替换掉，用户会丢功能。
 * 挂在原生 UI 上则是纯增量。而且因为扩展本身就在 shell 进程内，取像素走的是 shell
 * 内部接口，不经过 xdg-desktop-portal，所以没有 portal 的授权对话框问题。
 *
 * 交互设计（按钮点下去到底发生什么）
 * ----------------------------------
 *   已经自己拖过框        → 直接识别那块区域
 *   还没拖过（只有默认框）→ 按钮变成「请框选…」，等用户拖一次，松手立刻识别
 *                          这就是"独立的框选"
 *   屏幕 / 窗口模式       → 直接识别整屏 / 该窗口
 *   正在等框选时再点一次  → 取消，回到「提取文字」
 *
 * 刻意**不**复用"打开时就有的那个默认选区"：原生 UI 一打开，`reset()` 就会在屏幕
 * 正中央塞一个约 1/4 屏大的默认框（screenshot.js:324-327），拿它直接做 OCR 几乎
 * 肯定不是用户想要的东西，所以必须先让用户自己框一次。
 *
 * 反馈为什么写在按钮文字上而不是发通知
 * ------------------------------------
 * 截图 UI 打开时处于模态抓取状态，`Main.notify` 弹不出来（要等 UI 关掉才出现）——
 * 上一版"点了没反应、退出后才蹦提示"就是这么来的。所以等待/失败这类**即时**反馈
 * 一律写在按钮标签上；`Main.notify` 只作为成功/失败的留档。
 *
 * 依赖的原生内部结构（GNOME 50 / screenshot.js，共 3142 行）
 * ----------------------------------------------------------
 *   export const ScreenshotUI                    行 1078
 *   this._typeButtonContainer                    行 1265  ← 装「区域/屏幕/窗口」三按钮
 *   IconLabelButton / screenshot-ui-type-button  行  53   ← 按钮的样式与内部结构
 *   UIAreaSelector.getGeometry()                 行 334   ← 选区 → [x,y,w,h]
 *   UIAreaSelector 信号 drag-started/drag-ended  行 254
 *   this._stageScreenshot                        行 1173  ← 冻结帧，取像素的来源
 *   ScreenshotUI._getSelectedGeometry()          行 1871
 *   Shell.Screenshot.composite_to_stream()       行 2416  ← 真正的取像素调用
 *
 * 有个坑记在这里：选区的坐标要用 UIAreaSelector.getGeometry()，**不是** `_result`
 * ——`_result` 属于另一个类 SelectArea（D-Bus SelectArea 那套），UIAreaSelector 上
 * 根本没这个字段。用错的话每点必失败，还会误报"请先框选"。
 *
 * 这些都是私有字段，GNOME 换版本时可能改名——这是所有扩展的通病。
 * 所以每一处访问都做了存在性判断：真改名了，按钮会给出提示而不是把 shell 弄崩。
 *
 * 取像素为什么不重新截屏
 * ----------------------
 * 原生 UI 打开时屏幕已经被拍成一张冻结帧画在 `_stageScreenshot` 上。这时候再截一次
 * 屏，拍到的是"带着遮罩和面板的画面"，不是用户想识别的内容。所以必须从冻结帧的
 * texture 上按选区裁剪——也就是照抄原生"保存截图"的取像素路径。
 *
 * 识别怎么跑
 * ----------
 * 把裁剪出的 PNG 落成临时文件，调 `bin/ocr` 走常驻服务（RapidOCR，本地，不联网），
 * 拿到文字塞进剪贴板，发通知，然后关掉 UI——和原生快门按钮点完就关的行为一致。
 * 用子进程而不是在扩展里直连 socket，是为了复用已经跑通的那套客户端（含守护进程
 * 自动拉起、错误信息），少一份要维护的协议实现。
 */

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Shell from 'gi://Shell';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {ScreenshotUI} from 'resource:///org/gnome/shell/ui/screenshot.js';

// `communicate_utf8_async` 没有现成的 Promise 版本，直接 await 会报
//   "At least 3 arguments required, but only 2 passed"
// ——它要一个 callback，而 gnome-shell 并没有 promisify 它（只 promisify 了
// screenshot.js 里自己用的那几个）。这里自己补上；`_promisify` 是幂等的。
//
// 另一个坑：promisify 之后返回值是 [stdout, stderr] **两项**，不像
// `communicate_utf8_finish()` 那样开头还有个 success 布尔。写成
// `const [, stdout, stderr] = ...` 会把 stderr 当成 stdout——识别结果永远是空的，
// 而且错误信息全被吃掉，只看到"没识别到文字"。
Gio._promisify(Gio.Subprocess.prototype, 'communicate_utf8_async');

/** 挂在 UI 实例上的属性名，用来判断"是否已经加过"。 */
const BUTTON_PROP = '_prtscOcrButton';
const DRAG_ID_PROP = '_prtscOcrDragId';
const DRAGGED_PROP = '_prtscOcrDragged';

/** 图标：Yaru 里有（经 Yaru-blue-dark → Yaru-blue → Yaru 继承链解析得到）。 */
const ICON_NAME = 'insert-text-symbolic';
const LABEL_IDLE = '提取文字';
const LABEL_ARMED = '请框选…';
const LABEL_BUSY = '识别中…';

/** 提示信息在按钮上停留的秒数。 */
const HINT_SECONDS = 4;

/** bin/ocr 的退出码约定。 */
const OCR_EXIT_OK = 0;
const OCR_EXIT_NO_TEXT = 2;

/** 宽或高小于这个像素数就算"没真框"，避免手抖点一下就拿几像素去识别。 */
const MIN_SELECTION = 8;

/**
 * 没有配置 OCR 路径时，按顺序找这几个位置，用第一个真的存在的。
 *
 * 项目目录名和仓库名不一致（仓库叫 WaylandOCR，而安装文档一路用的是
 * ~/PrtScOCR），所以两种都认：clone 下来没改名的人也能直接用。
 * 想固定的话在首选项里填死路径即可。
 */
const OCR_PATH_CANDIDATES = [
    ['PrtScOCR', 'bin', 'ocr'],
    ['WaylandOCR', 'bin', 'ocr'],
].map(parts => GLib.build_filenamev([GLib.get_home_dir(), ...parts]));

/** 保存原生 open()，disable 时原样还回去。模块级是为了防止重复包装。 */
let originalOpen = null;

function logInfo(msg) {
    // 用 shell 的全局 log()：console.debug 走 G_LOG_LEVEL_DEBUG，默认被过滤掉，
    // 排查问题时会以为扩展没加载。
    log(`[prtsc-ocr] ${msg}`);
}

/** 把异常压成能塞进按钮标签的短句。 */
function shortReason(e) {
    const msg = `${e.message ?? e}`;
    return msg.length > 14 ? `${msg.slice(0, 14)}…` : msg;
}

/**
 * 造一个和原生「区域/屏幕/窗口」长得一样的按钮。
 *
 * 原生用的是模块内未导出的 IconLabelButton（竖排：图标 + 居中标签，外层样式类
 * `screenshot-ui-type-button`，内层 `icon-label-button-container`）。这里按同一个
 * 结构手工搭一遍，样式类一致 → 渲染结果一致，且不依赖那个未导出的类。
 */
function makeTypeButton(onClicked) {
    const button = new St.Button({
        style_class: 'screenshot-ui-type-button',
        x_expand: true,
        can_focus: true,
    });

    const box = new St.BoxLayout({
        orientation: Clutter.Orientation.VERTICAL,
        style_class: 'icon-label-button-container',
    });
    button.set_child(box);

    box.add_child(new St.Icon({icon_name: ICON_NAME}));

    const label = new St.Label({
        text: LABEL_IDLE,
        x_align: Clutter.ActorAlign.CENTER,
    });
    box.add_child(label);
    button._prtscLabel = label;

    button.connect('clicked', onClicked);
    return button;
}

export default class PrtscOcrExtension extends Extension {
    enable() {
        this._busy = false;
        this._armed = false;
        this._hintId = 0;

        // 首选项里的 OCR 程序路径。schema 缺失时（例如用软链接装进来但没编译
        // schema）不能连累整个扩展——退化成默认路径照常用。
        try {
            this._settings = this.getSettings();
        } catch (e) {
            logError(e, 'prtsc-ocr: 读不到设置 schema，改用默认 OCR 路径');
            this._settings = null;
        }

        // 包装 open()：每次 UI 打开都确保按钮在、并把状态复位。
        // 不能只包装 _init——Main.screenshotUI 这个单例在 shell 启动时就 new 好了
        // （main.js:260），那时 _init 早跑完了，扩展根本来不及插手。
        if (!originalOpen)
            originalOpen = ScreenshotUI.prototype.open;

        const ext = this;
        ScreenshotUI.prototype.open = async function (...args) {
            const result = await originalOpen.apply(this, args);
            try {
                ext._onUIOpened(this);
            } catch (e) {
                logError(e, 'prtsc-ocr: 准备按钮失败');
            }
            return result;
        };

        // 单例已经存在，立刻补一次（正常情况走的就是这条）。
        try {
            this._ensureButton(Main.screenshotUI);
        } catch (e) {
            logError(e, 'prtsc-ocr: 添加按钮失败');
        }

        logInfo('已启用');
    }

    disable() {
        if (originalOpen) {
            ScreenshotUI.prototype.open = originalOpen;
            originalOpen = null;
        }

        if (this._hintId) {
            GLib.source_remove(this._hintId);
            this._hintId = 0;
        }

        const ui = Main.screenshotUI;
        if (ui) {
            for (const id of ui[DRAG_ID_PROP] ?? []) {
                try {
                    ui._areaSelector?.disconnect(id);
                } catch {
                    // 界面已经销毁的情况，忽略
                }
            }
            delete ui[DRAG_ID_PROP];
            delete ui[DRAGGED_PROP];

            // 别把用户的原生界面留在"框被藏掉"的状态里
            this._setSelectionVisible(ui, true);

            const button = ui[BUTTON_PROP];
            if (button) {
                delete ui[BUTTON_PROP];
                button.destroy();
            }
        }

        this._armed = false;
        this._busy = false;
        this._settings = null;
        logInfo('已停用');
    }

    // ---------------------------------------------------------------- 界面准备

    /** UI 每次打开：确保按钮在、状态复位。 */
    _onUIOpened(ui) {
        this._ensureButton(ui);

        this._armed = false;
        if (this._hintId) {
            GLib.source_remove(this._hintId);
            this._hintId = 0;
        }

        ui[DRAGGED_PROP] = false;

        // 上一次可能停在"等框选"状态、把框藏了；每次打开都还原成原生样子
        this._setSelectionVisible(ui, true);

        const button = ui[BUTTON_PROP];
        if (button) {
            this._setLabel(button, LABEL_IDLE);
            button.reactive = true;
        }
    }

    /** 给某个 ScreenshotUI 实例挂上按钮；重复调用是安全的。 */
    _ensureButton(ui) {
        if (!ui || ui[BUTTON_PROP])
            return;

        const container = ui._typeButtonContainer;
        if (!container) {
            logInfo('找不到 _typeButtonContainer，跳过（GNOME 内部结构可能变了）');
            return;
        }

        const button = makeTypeButton(() => {
            this._onButtonClicked(ui, button);
        });

        container.add_child(button);
        ui[BUTTON_PROP] = button;

        this._hookAreaSelector(ui);

        logInfo('按钮已加入原生截图工具栏');
    }

    /**
     * 盯住原生选区：
     *   drag-started → 把选区外观放出来（拖动时必须有实时反馈）
     *   drag-ended   → 记下"用户确实自己拖过框"；若正在等框选且框得够大，就直接识别
     *
     * `drag-ended` 是在 `stopDrag()` 里发的（screenshot.js:443），而坐标在
     * `_onMotion` 过程中就已更新，所以信号到达时 getGeometry() 拿到的就是最终选区。
     */
    _hookAreaSelector(ui) {
        const selector = ui._areaSelector;
        if (!selector || ui[DRAG_ID_PROP])
            return;

        const ids = [];

        ids.push(selector.connect('drag-started', () => {
            this._setSelectionVisible(ui, true);
        }));

        ids.push(selector.connect('drag-ended', () => {
            ui[DRAGGED_PROP] = true;

            const button = ui[BUTTON_PROP];
            if (!button || !this._armed || this._busy)
                return;

            // 只是点了一下、没真拖开：别拿几像素的框去识别，继续等他框
            if (!this._selectionRect(ui)) {
                this._setSelectionVisible(ui, false);
                return;
            }

            this._runOcr(ui, button);
        }));

        ui[DRAG_ID_PROP] = ids;
    }

    /**
     * 显示 / 隐藏选区外观：那个"框"（四角把手 + 边框）和四周的压暗遮罩。
     *
     * 原生代码从不碰这几个 actor 的可见性（整个 UIAreaSelector 里没有一处
     * show()/hide()），所以这里藏了就会一直藏着，得自己负责还原。
     * 藏的时候连遮罩一起藏：只藏边框的话，中间那块亮的矩形还是个"框"。
     */
    _setSelectionVisible(ui, visible) {
        const selector = ui._areaSelector;
        if (!selector)
            return;

        const actors = [
            selector._areaIndicator,
            selector._topLeftHandle,
            selector._topRightHandle,
            selector._bottomLeftHandle,
            selector._bottomRightHandle,
        ];

        for (const actor of actors) {
            if (!actor)
                continue;
            if (visible)
                actor.show();
            else
                actor.hide();
        }
    }

    /** 当前选区 [x,y,w,h]；没框、或框得比 MIN_SELECTION 还小时返回 null。 */
    _selectionRect(ui) {
        const selector = ui._areaSelector;
        if (!selector)
            return null;

        const [x, y, w, h] = selector.getGeometry();
        if (!Number.isFinite(x) || !Number.isFinite(y) ||
            w < MIN_SELECTION || h < MIN_SELECTION)
            return null;

        return [x, y, w, h];
    }

    // -------------------------------------------------------------------- 交互

    _onButtonClicked(ui, button) {
        if (this._busy)
            return;

        // 正在等框选，再点一次 = 取消
        if (this._armed) {
            this._armed = false;
            this._setSelectionVisible(ui, true);
            this._setLabel(button, LABEL_IDLE);
            return;
        }

        // 选择模式且用户还没自己框过：进入"等你框选"状态。
        // 同时把打开时那个默认框藏掉——不留一个占位框在那儿碍事，
        // 用户拖的时候 drag-started 会把框放回来做实时反馈。
        if (ui._selectionButton?.checked && !ui[DRAGGED_PROP]) {
            this._armed = true;
            this._setSelectionVisible(ui, false);
            this._setLabel(button, LABEL_ARMED);
            return;
        }

        this._runOcr(ui, button);
    }

    /** 完整流程：取像素 → 识别 → 剪贴板 → 通知 → 关界面。异常自己吞掉。 */
    async _runOcr(ui, button) {
        if (this._busy)
            return;

        this._busy = true;
        this._armed = false;
        button.reactive = false;

        // 之前的临时提示要撤掉，否则它的定时器会把"识别中…"冲掉
        if (this._hintId) {
            GLib.source_remove(this._hintId);
            this._hintId = 0;
        }

        this._setLabel(button, LABEL_BUSY);

        try {
            const bytes = await this._grabSelection(ui);
            if (!bytes) {
                this._hint(button, '还没框选');
                return;
            }

            const text = await this._ocrBytes(bytes);
            if (!text) {
                this._hint(button, '没识别到文字');
                return;
            }

            const chars = [...text].length;
            const preview = chars > 60 ? `${[...text].slice(0, 60).join('')}…` : text;

            // 剪贴板立刻写——这是主路径，用户切过去粘贴时必须已经就绪。
            St.Clipboard.get_default().set_text(St.ClipboardType.CLIPBOARD, text);

            // 但通知要等界面关掉之后再发：模态 grab 期间 Main.notify 根本显示不出来，
            // 原来在 grab 里发等于白弹。原生 close() 是先 _grabHelper.ungrab()、
            // 再在收尾时 emit('closed')（screenshot.js:1716），所以挂 'closed' 正好。
            this._afterClose(ui, () => {
                try {
                    this._notify('提取文字', `已复制 ${chars} 个字符：${preview}`);
                } catch (e) {
                    logError(e, 'prtsc-ocr: 关闭后发通知失败');
                }
            });

            // 和原生快门按钮一致：干完活就关掉
            ui.close();
        } catch (e) {
            logError(e, 'prtsc-ocr');
            this._hint(button, shortReason(e));
            this._notify('提取文字失败', `${e.message ?? e}`);
        } finally {
            this._busy = false;
            button.reactive = true;
            // 没有留下临时提示才复位；有提示的话让 _hint 的定时器负责复位
            if (this._hintId === 0)
                this._setLabel(button, LABEL_IDLE);
        }
    }

    /**
     * 等截图界面真正关掉（模态 grab 已释放）再执行 action。
     *
     * 'closed' 是原生 ScreenshotUI 的信号（signals 定义在 screenshot.js:1087，
     * 收尾处 emit，见 1716）。万一它不来（例如界面已经被别的路径关掉了），用一条
     * 兜底定时器保证 action 照样执行，而且只执行一次。
     */
    _afterClose(ui, action) {
        let done = false;
        let closedId = 0;
        let fallbackId = 0;

        const run = () => {
            if (done)
                return;
            done = true;

            if (closedId) {
                try {
                    ui.disconnect(closedId);
                } catch {
                    // 已经断开了，忽略
                }
                closedId = 0;
            }
            if (fallbackId) {
                GLib.source_remove(fallbackId);
                fallbackId = 0;
            }

            action();
        };

        closedId = ui.connect('closed', run);
        fallbackId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 500, () => {
            fallbackId = 0;
            run();
            return GLib.SOURCE_REMOVE;
        });
    }

    _setLabel(button, text) {
        const label = button?._prtscLabel;
        if (label && label.text !== text)
            label.text = text;
    }

    /** 在按钮上显示一条临时提示（UI 打开时通知弹不出来，只能写这里）。 */
    _hint(button, text) {
        this._setLabel(button, text);

        if (this._hintId)
            GLib.source_remove(this._hintId);

        this._hintId = GLib.timeout_add_seconds(
            GLib.PRIORITY_DEFAULT, HINT_SECONDS, () => {
                this._hintId = 0;
                this._setLabel(button, LABEL_IDLE);
                return GLib.SOURCE_REMOVE;
            });
    }

    _notify(title, body) {
        Main.notify(title, body);
    }

    // ------------------------------------------------------------------ 取像素

    /**
     * 从冻结帧里裁出选区像素，返回 PNG 字节。
     *
     * 返回 null 专指"用户还没框选"。抓屏本身出问题一律抛异常，这样按钮上的提示
     * 能说清到底是哪种情况。
     */
    async _grabSelection(ui) {
        let texture = null;
        let geometry = null;
        let scale = 1;

        if (ui._windowButton?.checked) {
            // 窗口模式：拿被选中窗口的 texture，整窗，不做裁剪
            const win = ui._windowSelectors
                ?.flatMap(selector => selector.windows())
                .find(w => w.checked);
            const content = win?.windowContent;
            if (!content)
                throw new Error('没选中窗口，或拿不到窗口画面');
            texture = content.get_texture();
            scale = win.bufferScale ?? 1;
            geometry = null;
        } else {
            const content = ui._stageScreenshot?.get_content();
            if (!content)
                throw new Error('没拿到屏幕冻结帧');
            texture = content.get_texture();
            scale = ui._scale ?? 1;

            if (ui._selectionButton?.checked) {
                // 用 UIAreaSelector.getGeometry()（[x,y,w,h]）。
                // 别去找 _result —— 那是另一个类 SelectArea 的字段。
                const rect = this._selectionRect(ui);
                if (!rect)
                    return null;
                geometry = rect.map(v => v * scale);
            } else {
                // 整屏模式
                geometry = ui._getSelectedGeometry(true);
            }
        }

        if (!texture)
            throw new Error('拿到的画面是空的');

        const stream = Gio.MemoryOutputStream.new_resizable();
        const [x, y, w, h] = geometry ?? [0, 0, -1, -1];
        // 光标传 null：一个箭头压在字上会直接影响识别
        await Shell.Screenshot.composite_to_stream(
            texture, x, y, w, h, scale, null, 0, 0, 1, stream);
        stream.close(null);
        return stream.steal_as_bytes();
    }

    /**
     * 当前该用哪个 OCR 程序。
     *
     * 首选项里留空就用项目默认路径；读设置出错也回退到默认值——
     * 绝不能因为读不到一个设置就让整个扩展失效。
     */
    _resolveOcrBin() {
        try {
            const custom = this._settings?.get_string('ocr-command')?.trim();
            if (custom)
                return custom;
        } catch (e) {
            logError(e, 'prtsc-ocr: 读 ocr-command 失败，用默认路径');
        }

        for (const candidate of OCR_PATH_CANDIDATES) {
            if (GLib.file_test(candidate, GLib.FileTest.IS_EXECUTABLE))
                return candidate;
        }

        // 一个都不存在就报第一个，让错误信息指向文档里写的那个位置
        return OCR_PATH_CANDIDATES[0];
    }

    /** 把 PNG 字节交给常驻 OCR 服务，返回识别出的文字。 */
    async _ocrBytes(bytes) {
        const ocrBin = this._resolveOcrBin();

        if (!GLib.file_test(ocrBin, GLib.FileTest.IS_EXECUTABLE))
            throw new Error(`找不到 ${ocrBin}`);

        const [tmpFile] = Gio.File.new_tmp('prtsc-ocr-ext-XXXXXX.png');
        const path = tmpFile.get_path();

        try {
            GLib.file_set_contents(path, bytes.get_data());

            const proc = Gio.Subprocess.new(
                [ocrBin, '--quiet', path],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE);

            // [stdout, stderr] —— 两项，没有 success 布尔（见文件开头说明）
            const [stdout, stderr] = await proc.communicate_utf8_async(null, null);

            const status = proc.get_exit_status();

            // bin/ocr 的退出码约定（见 README §3）：
            //   0 成功 / 1 失败 / 2 图没问题但没识别到文字 / 3 服务不可用
            // 2 不是错误，是"这块地方没字"，返回空串让调用方提示"没识别到文字"。
            if (status === OCR_EXIT_NO_TEXT)
                return '';

            if (status !== OCR_EXIT_OK) {
                const lastLine = (stderr ?? '')
                    .trim().split('\n').filter(Boolean).pop();
                throw new Error(lastLine || `OCR 退出码 ${status}`);
            }
            return (stdout ?? '').trim();
        } finally {
            try {
                tmpFile.delete(null);
            } catch {
                // 临时文件删不掉不影响结果
            }
        }
    }
}
