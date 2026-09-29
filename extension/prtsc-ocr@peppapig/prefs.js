// SPDX-License-Identifier: GPL-3.0-or-later
//
// 「提取文字」按钮的首选项。目前只有一项：OCR 程序放在哪儿。
//
// 为什么需要它：扩展本身只是个 UI，识别是交给外部程序做的。默认路径
// ~/PrtScOCR/bin/ocr 是本项目的约定，别人从 extensions.gnome.org 装了扩展之后
// 得能把它指到自己那套后端上，否则扩展装了也没用。

import Adw from 'gi://Adw';
import GLib from 'gi://GLib';

import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

/** 和 extension.js 里的候选列表保持一致。 */
const OCR_PATH_CANDIDATES = [
    ['PrtScOCR', 'bin', 'ocr'],
    ['WaylandOCR', 'bin', 'ocr'],
].map(parts => GLib.build_filenamev([GLib.get_home_dir(), ...parts]));

export default class PrtscOcrPreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        const settings = this.getSettings();

        const page = new Adw.PreferencesPage({
            title: '截图 OCR',
        });

        const group = new Adw.PreferencesGroup({
            title: '识别引擎',
            description:
                '「提取文字」按钮会把框选的画面交给这个程序，' +
                '调用方式是 `<程序> --quiet <图片路径>`，识别结果从标准输出读取。\n' +
                `留空则自动在下面这些位置里找：\n${OCR_PATH_CANDIDATES.map(p => '　　' + p).join('\n')}`,
        });

        const row = new Adw.EntryRow({
            title: 'OCR 程序路径',
            show_apply_button: true,
        });
        row.text = settings.get_string('ocr-command');

        // 用「应用」按钮而不是实时写回，免得边打字边触发（打一半的路径是无效的）
        row.connect('apply', () => {
            settings.set_string('ocr-command', row.text.trim());
        });

        // 外部改动（gsettings / dconf）时同步回来
        settings.connect('changed::ocr-command', () => {
            const value = settings.get_string('ocr-command');
            if (value !== row.text)
                row.text = value;
        });

        group.add(row);
        page.add(group);
        window.add(page);
    }
}
