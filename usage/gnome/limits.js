// SPDX-License-Identifier: MIT
// Top bar presentation rules, free of GNOME imports so they can be tested anywhere.

const ISSUES = {
    disconnected: () => 'Disconnected',
    login_required: () => 'Not connected',
    unsupported_account: () => 'Subscription required',
    unsupported_version: name => `Update ${name}`,
    timeout: () => 'Connection timed out',
};
// Agent IDs name icon files, so only plain identifiers are accepted.
const AGENT_ID = /^[a-z0-9][a-z0-9_-]{0,63}$/;
const COLOR = /^#[0-9a-f]{6}$/i;
const LEVELS = {warning: '#f8e45c', critical: '#ff7b63'};

export function limitLabel(minutes) {
    return minutes === 10080 ? 'Weekly' : minutes === 1440 ? 'Daily'
        : minutes % 60 === 0 ? `${minutes / 60}-hour` : `${minutes}-minute`;
}

export function countdown(seconds) {
    const minutes = Math.max(0, Math.ceil(seconds / 60));
    return minutes >= 1440 ? `${Math.floor(minutes / 1440)}d ${Math.floor(minutes % 1440 / 60)}h`
        : minutes >= 60 ? `${Math.floor(minutes / 60)}h ${minutes % 60}m` : `${minutes}m`;
}

function level(used, warning) {
    if (used >= 100)
        return 'critical';
    return warning > 0 && used >= warning ? 'warning' : 'normal';
}

/** Whether a published body is the app's top bar payload. */
export function accepts(payload) {
    return Array.isArray(payload?.agents);
}

/** Agents with their limits for the menu, and the selected limits shown in the bar. */
export function summarize(payload, now, selection = []) {
    const warning = Number.isInteger(payload?.warning) ? payload.warning : 0;
    const agents = (Array.isArray(payload?.agents) ? payload.agents : []).flatMap(agent => {
        if (!AGENT_ID.test(agent?.id ?? '') || typeof agent.name !== 'string')
            return [];
        const windows = (Array.isArray(agent.limits) ? agent.limits : [])
            .filter(item => typeof item?.id === 'string' &&
                [item.minutes, item.used_percent, item.resets_at].every(Number.isFinite) &&
                item.resets_at > now)
            .sort((a, b) => a.minutes - b.minutes);
        if (!windows.length && agent.issue === 'not_found')
            return [];
        const available = agent.status === 'available';
        const color = COLOR.test(agent.color ?? '') ? agent.color : '#9a9a9a';
        const buckets = new Set(windows.map(item => item.bucket)).size > 1;
        const limits = windows.map(item => {
            const state = available ? level(item.used_percent, warning) : 'normal';
            return {
                id: item.id,
                agent: agent.id,
                label: `${buckets ? `${item.bucket} · ` : ''}${limitLabel(item.minutes)} limit`,
                used: item.used_percent,
                value: `${Math.floor(item.used_percent)}%`,
                level: state,
                fill: LEVELS[state] ?? color,
                stale: agent.status === 'stale',
                resetsAt: item.resets_at,
                reset: `Resets in ${countdown(item.resets_at - now)}`,
                selected: selection.includes(item.id),
            };
        });
        const issue = ISSUES[agent.issue] ?? (() => 'Limits unavailable');
        return [{
            id: agent.id,
            name: agent.name,
            note: agent.status === 'stale' ? 'Outdated' : limits.length ? '' : issue(agent.name),
            limits,
        }];
    }).sort((a, b) => a.name.localeCompare(b.name));
    return {agents, shown: agents.flatMap(agent => agent.limits.filter(item => item.selected))};
}
