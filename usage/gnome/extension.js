// SPDX-License-Identifier: MIT
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import St from 'gi://St';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

import {accepts, countdown, summarize} from './limits.js';

const INTERFACE = `<node><interface name="org.agentusage.Desktop">
  <method name="SetPinned"><arg type="i" direction="in"/><arg type="b" direction="in"/><arg type="b" direction="out"/></method>
  <method name="PrepareMove"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
  <method name="SetLimits"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="b" direction="out"/></method>
  <signal name="Command"><arg type="i"/><arg type="s"/></signal>
</interface></node>`;
// The app republishes at least every minute; older limits mean it stopped.
const STALE_SECONDS = 180;

export default class AgentUsageDesktop extends Extension {
    enable() {
        this._settings = this.getSettings();
        this._windows = new Map();
        this._map = global.window_manager.connect('map', (_, actor) => {
            this._track(actor.meta_window);
            // Keep the widget above desktop icons, within the desktop layer.
            if (actor.meta_window.get_window_type() === Meta.WindowType.DESKTOP)
                for (const window of this._windows.keys()) window.raise();
        });
        this._monitors = Main.layoutManager.connect('monitors-changed', () => {
            for (const window of this._windows.keys()) this._place(window);
        });
        this._grabEnd = global.display.connect('grab-op-end', (_, window) => {
            if (this._windows.has(window)) this._apply(window);
        });
        this._grabBegin = global.display.connect('grab-op-begin', (_, window) => {
            const state = this._windows.get(window);
            if (state?.moveTimer) {
                GLib.Source.remove(state.moveTimer);
                state.moveTimer = 0;
            }
        });
        this._limits = null;
        this._owner = 0;
        this._reveal = 0;
        this._rebuild = 0;
        this._indicator = new PanelMenu.Button(0.5, 'Agent Usage');
        this._panel = new St.BoxLayout({style_class: 'agent-usage-panel'});
        this._indicator.add_child(this._panel);
        this._indicator.visible = false;
        Main.panel.addToStatusArea(this.uuid, this._indicator, 0, 'right');
        // Rebuild once the menu has faded out, so it never reopens with old choices.
        this._indicator.menu.connect('open-state-changed', (_, open) => {
            if (open || this._rebuild) return;
            this._rebuild = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 250, () => {
                this._rebuild = 0;
                this._render();
                return GLib.SOURCE_REMOVE;
            });
        });
        this._settingsChanged = ['changed::panel-limits', 'changed::panel-reset-time']
            .map(signal => this._settings.connect(signal, () => this._render()));
        // Countdowns and staleness advance without new data.
        this._tick = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 30, () => {
            this._render();
            return GLib.SOURCE_CONTINUE;
        });
        this._bus = Gio.DBusExportedObject.wrapJSObject(INTERFACE, this);
        this._bus.export(Gio.DBus.session, '/org/agentusage/Desktop');
        for (const actor of global.get_window_actors()) this._track(actor.meta_window);
    }

    _icon(name, style_class) {
        let path = GLib.build_filenamev([this.path, 'icons', `${name}-symbolic.svg`]);
        if (!GLib.file_test(path, GLib.FileTest.EXISTS))
            path = GLib.build_filenamev([this.path, 'icons', 'agent-usage-symbolic.svg']);
        return new St.Icon({gicon: Gio.icon_new_for_string(path), style_class});
    }

    _label(text, style_class = '', muted = false) {
        return new St.Label({text, style_class, opacity: muted ? 178 : 255, y_align: Clutter.ActorAlign.CENTER});
    }

    _render() {
        if (this._limits && Date.now() / 1000 - this._received > STALE_SECONDS) this._limits = null;
        this._indicator.visible = this._limits !== null;
        if (!this._limits) {
            this._indicator.menu.close();
            return;
        }
        const now = Date.now() / 1000;
        const summary = summarize(this._limits, now, this._settings.get_strv('panel-limits'));
        const times = this._settings.get_boolean('panel-reset-time');
        this._panel.destroy_all_children();
        for (const limit of summary.shown) {
            const group = new St.BoxLayout({style_class: 'agent-usage-group', opacity: limit.stale ? 128 : 255});
            group.add_child(this._icon(limit.agent, 'system-status-icon'));
            group.add_child(this._label(limit.value, `agent-usage-${limit.level}`));
            if (times) {
                group.add_child(this._icon('clock', 'system-status-icon agent-usage-clock'));
                group.add_child(this._label(countdown(limit.resetsAt - now), '', true));
            }
            this._panel.add_child(group);
        }
        if (!summary.shown.length) this._panel.add_child(this._icon('agent-usage', 'system-status-icon'));
        const names = new Map(summary.agents.map(agent => [agent.id, agent.name]));
        this._indicator.accessible_name = ['Agent Usage', ...summary.shown.map(limit =>
            `${names.get(limit.agent)} ${limit.label} ${limit.value}, resets in ${countdown(limit.resetsAt - now)}`)]
            .join('; ');
        // Rebuilding an open menu would pull items from under the pointer.
        if (!this._indicator.menu.isOpen) this._fillMenu(summary);
    }

    _fillMenu({agents}) {
        const menu = this._indicator.menu;
        menu.removeAll();
        for (const {id, name, note, limits} of agents) {
            const heading = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
            const label = this._label(name, 'agent-usage-agent');
            label.x_expand = true;
            heading.add_child(this._icon(id, 'popup-menu-icon'));
            heading.add_child(label);
            if (note) heading.add_child(this._label(note, '', true));
            menu.addMenuItem(heading);
            for (const limit of limits) menu.addMenuItem(this._limitItem(limit));
        }
        if (agents.length) {
            const times = new PopupMenu.PopupSwitchMenuItem('Show reset time',
                this._settings.get_boolean('panel-reset-time'));
            times.connect('toggled', (_, state) => this._settings.set_boolean('panel-reset-time', state));
            // Toggling in place keeps the menu open.
            times.activate = () => times.toggle();
            menu.addMenuItem(times);
            menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        }
        const {visible} = this._limits;
        menu.addAction(visible ? 'Hide widget' : 'Show widget', () => {
            if (!visible) this._reveal = Date.now();
            this._command('toggle');
        });
        menu.addAction('Quit', () => this._command('quit'));
    }

    // Checking a limit shows it in the top bar; the menu stays open to choose several.
    _limitItem({id, label, value, level, used, fill, reset, selected}) {
        const item = new PopupMenu.PopupBaseMenuItem({style_class: 'agent-usage-limit'});
        item.setOrnament(selected ? PopupMenu.Ornament.CHECK : PopupMenu.Ornament.NONE);
        const rows = new St.BoxLayout({
            style_class: 'agent-usage-rows', orientation: Clutter.Orientation.VERTICAL, x_expand: true,
        });
        const heading = new St.BoxLayout({x_expand: true});
        const name = this._label(label);
        name.x_expand = true;
        heading.add_child(name);
        heading.add_child(this._label(value, `agent-usage-value agent-usage-${level}`));
        const track = new St.Widget({style_class: 'agent-usage-track'});
        track.add_child(new St.Widget({
            style_class: 'agent-usage-fill',
            style: `width: ${Math.round(Math.min(Math.max(used, 0), 100) * 2.4)}px; background-color: ${fill};`,
        }));
        rows.add_child(heading);
        rows.add_child(track);
        rows.add_child(this._label(reset, '', true));
        item.add_child(rows);
        item.activate = () => {
            const selection = this._settings.get_strv('panel-limits');
            const chosen = !selection.includes(id);
            this._settings.set_strv('panel-limits',
                chosen ? [...selection, id] : selection.filter(entry => entry !== id));
            item.setOrnament(chosen ? PopupMenu.Ornament.CHECK : PopupMenu.Ornament.NONE);
        };
        return item;
    }

    _command(action) {
        if (this._owner) this._bus.emit_signal('Command', new GLib.Variant('(is)', [this._owner, action]));
    }

    _track(window) {
        if (this._windows.has(window) || window.get_wm_class() !== 'agent-usage' ||
            window.get_title() !== 'Agent Usage' || window.get_transient_for()) return;
        const state = {
            type: window.get_window_type(), sticky: window.is_on_all_workspaces(),
            hidden: window.is_skip_taskbar(), pinned: window.is_above(), signals: [], timer: 0, moveTimer: 0,
        };
        this._windows.set(window, state);
        this._apply(window);
        this._place(window, this._settings.get_value('position').deepUnpack());
        state.signals.push(window.connect('size-changed', () => this._place(window)));
        state.signals.push(window.connect('position-changed', () => {
            if (state.timer) GLib.Source.remove(state.timer);
            state.timer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 300, () => {
                state.timer = 0;
                const {x, y} = window.get_frame_rect();
                this._settings.set_value('position', new GLib.Variant('(ii)', [x, y]));
                return GLib.SOURCE_REMOVE;
            });
        }));
        state.signals.push(window.connect('unmanaged', () => this._forget(window, false)));
        // A widget shown from the menu comes forward until it loses focus.
        if (Date.now() - this._reveal < 5000) {
            this._reveal = 0;
            window.set_type(Meta.WindowType.NORMAL);
            window.activate(global.get_current_time());
            const focus = window.connect('notify::appears-focused', () => {
                if (window.appears_focused) return;
                window.disconnect(focus);
                state.signals.splice(state.signals.indexOf(focus), 1);
                this._apply(window);
            });
            state.signals.push(focus);
        }
    }

    _apply(window) {
        const {pinned} = this._windows.get(window);
        window.set_type(pinned ? Meta.WindowType.NORMAL : Meta.WindowType.DESKTOP);
        if (pinned) window.make_above();
        else window.unmake_above();
        window.hide_from_window_list();
        window.stick();
        window.raise();
    }

    _place(window, saved = null) {
        const frame = window.get_frame_rect();
        let [x, y] = saved ?? [frame.x, frame.y];
        const monitor = Main.layoutManager.monitors.find(m =>
            x >= m.x && x < m.x + m.width && y >= m.y && y < m.y + m.height
        ) ?? Main.layoutManager.primaryMonitor;
        if (!monitor) return;
        const area = Main.layoutManager.getWorkAreaForMonitor(monitor.index);
        if (saved?.[0] === -1 && saved?.[1] === -1) {
            x = area.x + area.width - frame.width - 24;
            y = area.y + 24;
        }
        x = Math.max(area.x, Math.min(x, area.x + area.width - frame.width));
        y = Math.max(area.y, Math.min(y, area.y + area.height - frame.height));
        if (x !== frame.x || y !== frame.y) window.move_frame(true, x, y);
    }

    _find(pid) {
        return [...this._windows.keys()].find(window => window.get_pid() === pid);
    }

    SetPinned(pid, pinned) {
        const window = this._find(pid);
        if (!window) return false;
        this._windows.get(window).pinned = pinned;
        this._apply(window);
        return true;
    }

    PrepareMove(pid) {
        const window = this._find(pid);
        if (!window || !(global.get_pointer()[2] & Clutter.ModifierType.BUTTON1_MASK)) return false;
        // Let Qt start the native move using its Wayland input serial.
        window.set_type(Meta.WindowType.NORMAL);
        const state = this._windows.get(window);
        if (state.moveTimer) GLib.Source.remove(state.moveTimer);
        state.moveTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 1000, () => {
            state.moveTimer = 0;
            this._apply(window);
            return GLib.SOURCE_REMOVE;
        });
        return true;
    }

    SetLimits(pid, body) {
        // An empty body withdraws limits when their publisher quits.
        if (!body) {
            if (pid === this._owner) this._limits = null;
            this._render();
            return true;
        }
        let limits;
        try {
            limits = JSON.parse(body);
        } catch {
            return false;
        }
        if (!accepts(limits)) return false;
        this._limits = limits;
        this._owner = pid;
        this._received = Date.now() / 1000;
        this._render();
        return true;
    }

    _forget(window, restore) {
        const state = this._windows.get(window);
        if (state.timer) GLib.Source.remove(state.timer);
        if (state.moveTimer) GLib.Source.remove(state.moveTimer);
        for (const signal of state.signals) window.disconnect(signal);
        this._windows.delete(window);
        if (restore) {
            window.set_type(state.type);
            if (!state.hidden) window.show_in_window_list();
            if (!state.sticky) window.unstick();
        }
    }

    disable() {
        // Restore a hidden widget, unless the shell is only locking the screen.
        if (!Main.sessionMode.isLocked) this._command('detach');
        this._bus.unexport();
        this._bus = null;
        GLib.Source.remove(this._tick);
        if (this._rebuild) GLib.Source.remove(this._rebuild);
        for (const id of this._settingsChanged) this._settings.disconnect(id);
        this._indicator.destroy();
        this._indicator = this._panel = this._limits = null;
        global.window_manager.disconnect(this._map);
        global.display.disconnect(this._grabEnd);
        global.display.disconnect(this._grabBegin);
        Main.layoutManager.disconnect(this._monitors);
        for (const window of this._windows.keys()) this._forget(window, true);
        this._windows = null;
        this._settings = null;
    }
}
