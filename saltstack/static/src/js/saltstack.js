/** @odoo-module **/
/* SaltStack Infrastructure — client helpers */
/* Copyright (C) 2026 Vertel Sverige AB — License AGPL-3.0 */

import { registry } from "@web/core/registry";

// Copy a server-provided value to the clipboard (used by the IP copy button
// in the minion list and the storage summary).
async function saltstackCopyToClipboard(text) {
    if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return;
    }
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    try {
        document.execCommand("copy");
    } finally {
        document.body.removeChild(ta);
    }
}

registry.category("actions").add("saltstack_copy_value", async (env, action) => {
    try {
        await saltstackCopyToClipboard(action.params.value || "");
        env.services.notification.add("Kopierat till urklipp", { type: "success" });
    } catch (err) {
        console.warn("SaltStack: Clipboard copy failed", err);
        env.services.notification.add("Kopiering misslyckades", { type: "danger" });
    }
});
