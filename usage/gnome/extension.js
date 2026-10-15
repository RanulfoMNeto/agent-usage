// SPDX-License-Identifier: MIT
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const INTERFACE = `<node><interface name="org.agentusage.Desktop">
  <method name="SetPinned"><arg type="i" direction="in"/><arg type="b" direction="in"/><arg type="b" direction="out"/></method>
  <method name="PrepareMove"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
</interface></node>`;

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
        this._bus = Gio.DBusExportedObject.wrapJSObject(INTERFACE, this);
        this._bus.export(Gio.DBus.session, '/org/agentusage/Desktop');
        for (const actor of global.get_window_actors()) this._track(actor.meta_window);
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
        this._bus.unexport();
        this._bus = null;
        global.window_manager.disconnect(this._map);
        global.display.disconnect(this._grabEnd);
        global.display.disconnect(this._grabBegin);
        Main.layoutManager.disconnect(this._monitors);
        for (const window of this._windows.keys()) this._forget(window, true);
        this._windows = null;
        this._settings = null;
    }
}
