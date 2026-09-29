// SPDX-License-Identifier: GPL-3.0-or-later
//
// Preferences for the "Extract text" button. Currently a single setting:
// where the OCR program lives.
//
// Why it exists: the extension is only a UI — recognition is delegated to an
// external program. ~/PrtScOCR/bin/ocr is this project's convention, and anyone
// who installs the extension needs to be able to point it at their own backend,
// otherwise the extension is useless.
//
// All user-visible strings are gettext msgids in English; the Chinese text lives
// in po/zh_CN.po and is compiled into locale/ by bin/compile-locales.
// Only single string literals are passed to _() — no concatenation — so the
// msgids can be extracted reliably and matched against the .po files.

import Adw from 'gi://Adw';
import GLib from 'gi://GLib';

import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

/** Keep in sync with the candidate list in extension.js. */
const OCR_PATH_CANDIDATES = [
    ['PrtScOCR', 'bin', 'ocr'],
    ['WaylandOCR', 'bin', 'ocr'],
].map(parts => GLib.build_filenamev([GLib.get_home_dir(), ...parts]));

export default class PrtscOcrPreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        const settings = this.getSettings();

        // The prefs process is not the shell process: gettext is proxied over
        // D-Bus back to the extension instance. If that proxy is unavailable,
        // fall back to the English source string instead of breaking the page.
        const _ = str => {
            try {
                return this.gettext(str);
            } catch {
                return str;
            }
        };

        const page = new Adw.PreferencesPage({
            title: _('Screenshot OCR'),
        });

        const candidates = OCR_PATH_CANDIDATES.map(p => `　　${p}`).join('\n');

        const group = new Adw.PreferencesGroup({
            title: _('Recognition engine'),
            description:
                _('The “Extract text” button hands the selected area to this program.\nIt is called as `<program> --quiet <image path>`, and the result is read from stdout.\nExit code 0 means success, 2 means no text was found, anything else is a failure.') +
                '\n\n' +
                _('Leave this empty to search the following locations:') +
                '\n' +
                candidates,
        });

        const row = new Adw.EntryRow({
            title: _('OCR program path'),
            show_apply_button: true,
        });
        row.text = settings.get_string('ocr-command');

        // An explicit "Apply" button rather than writing on every keystroke —
        // a half-typed path is not a valid program.
        row.connect('apply', () => {
            settings.set_string('ocr-command', row.text.trim());
        });

        // Mirror changes made outside the dialog (gsettings / dconf).
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
