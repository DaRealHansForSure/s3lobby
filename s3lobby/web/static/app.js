// The theme toggle, and the live lobby's refresh. Every page works without this script; it only
// adds the toggle and keeps the live lobby current without reloading.

const LIVE_REFRESH_INTERVAL_MS = 3000;
// A page needs this many big sections before a bar to jump between them helps.
const MIN_SECTIONS_FOR_BAR = 2;

const root = document.documentElement;

function currentTheme() {
    const chosenTheme = root.getAttribute("data-theme");
    if (chosenTheme) {
        return chosenTheme;
    }
    if (window.matchMedia("(prefers-color-scheme: dark)").matches) {
        return "dark";
    }
    return "light";
}

function setUpThemeToggle() {
    const toggle = document.getElementById("theme-toggle");
    const label = document.getElementById("theme-label");
    if (!toggle || !label) {
        return;
    }

    function showCurrentTheme() {
        const isDark = currentTheme() === "dark";
        label.textContent = isDark ? "Light" : "Dark";
        toggle.setAttribute("aria-pressed", String(isDark));
        toggle.setAttribute("aria-label", isDark ? "Switch to light theme" : "Switch to dark theme");
    }

    toggle.addEventListener("click", () => {
        const nextTheme = currentTheme() === "dark" ? "light" : "dark";
        root.setAttribute("data-theme", nextTheme);
        try {
            localStorage.setItem("theme", nextTheme);
        } catch (error) {
            // Not fatal: the theme just does not survive a reload.
        }
        showCurrentTheme();
    });

    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
        if (!root.getAttribute("data-theme")) {
            showCurrentTheme();
        }
    });

    showCurrentTheme();
    // The button ships hidden: only a page running this script can switch themes.
    toggle.hidden = false;
}

// The admin menus are <details> and open and close without this script; it only keeps one open at
// a time and closes it on Escape or a click elsewhere.
function setUpNavigationMenus() {
    const menus = Array.from(document.querySelectorAll(".nav-menu"));
    if (menus.length === 0) {
        return;
    }
    for (const menu of menus) {
        menu.addEventListener("toggle", () => {
            if (!menu.open) {
                return;
            }
            for (const other of menus) {
                if (other !== menu) {
                    other.open = false;
                }
            }
        });
    }
    document.addEventListener("click", (event) => {
        for (const menu of menus) {
            if (!menu.contains(event.target)) {
                menu.open = false;
            }
        }
    });
    document.addEventListener("keydown", (event) => {
        if (event.key !== "Escape") {
            return;
        }
        for (const menu of menus) {
            menu.open = false;
        }
    });
}

// Lists on the board page that can be put in order by dragging their handles. Each list names the
// address its new order goes to; the order travels as the items' ids, top first. SortableJS
// (static/vendor) is loaded on those pages only; without it, their Up and Down buttons still work.
function setUpSortableLists() {
    const lists = document.querySelectorAll("[data-sortable-url]");
    if (lists.length === 0 || typeof window.Sortable === "undefined") {
        return;
    }
    const tokenField = document.querySelector("input[name=csrf_token]");
    for (const list of lists) {
        window.Sortable.create(list, {
            handle: ".drag-handle",
            animation: 150,
            onEnd: () => saveOrder(list, tokenField ? tokenField.value : ""),
        });
    }
}

// Sends a list's order; on any failure the page reloads, so it never shows an order the server
// did not keep.
function saveOrder(list, token) {
    const ids = Array.from(list.children)
        .map((item) => item.getAttribute("data-id"))
        .filter((id) => id !== null);
    const form = new FormData();
    form.append("csrf_token", token);
    form.append("ids", ids.join(","));
    fetch(list.getAttribute("data-sortable-url"), { method: "POST", body: form, credentials: "same-origin" })
        .then((response) => {
            if (!response.ok) {
                window.location.reload();
            }
        })
        .catch(() => window.location.reload());
}

// Forms marked data-confirm ask first: the page's confirm dialog shows the question and a button
// named like the action. Only that button submits; Cancel, the close button and Escape all leave
// the form unsent. Listening on the document also covers forms the live lobby swaps in later.
function setUpConfirmations() {
    const dialog = document.getElementById("confirm-dialog");
    if (!dialog || typeof dialog.showModal !== "function") {
        return;
    }
    const question = document.getElementById("confirm-question");
    const confirmButton = document.getElementById("confirm-button");
    let pendingForm = null;

    document.addEventListener("submit", (event) => {
        const form = event.target;
        if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-confirm")) {
            return;
        }
        if (form.dataset.confirmed === "yes") {
            return;
        }
        event.preventDefault();
        pendingForm = form;
        question.textContent = form.getAttribute("data-confirm");
        confirmButton.textContent = form.getAttribute("data-confirm-label") || "Confirm";
        dialog.returnValue = "";
        dialog.showModal();
    });
    for (const closer of dialog.querySelectorAll("[data-close]")) {
        closer.addEventListener("click", () => dialog.close("cancel"));
    }
    confirmButton.addEventListener("click", () => dialog.close("confirm"));
    // The action sits behind the return value, so Escape (which closes with an empty value) never
    // triggers it.
    dialog.addEventListener("close", () => {
        const form = pendingForm;
        pendingForm = null;
        if (dialog.returnValue !== "confirm" || !form) {
            return;
        }
        form.dataset.confirmed = "yes";
        form.requestSubmit();
    });
}

// Replaces the live part of the lobby page with a fresh copy rendered by the server. The server
// escapes every name in it, so swapping the HTML in is safe. Skipped while the tab is hidden.
function setUpLiveRefresh() {
    const liveState = document.getElementById("live-state");
    if (!liveState) {
        return;
    }
    const stateUrl = liveState.dataset.stateUrl;

    async function refreshLiveState() {
        if (document.hidden) {
            return;
        }
        try {
            const response = await fetch(stateUrl, { credentials: "same-origin", cache: "no-store" });
            if (response.ok) {
                liveState.innerHTML = await response.text();
                // The fresh copy has new sections without the anchors the jump bar links to.
                giveSectionsAnchors();
            }
        } catch (error) {
            // The web interface is unreachable for a moment; the next refresh tries again.
        }
    }

    window.setInterval(refreshLiveState, LIVE_REFRESH_INTERVAL_MS);
    document.addEventListener("visibilitychange", refreshLiveState);
}

// The page's big sections: every section whose head has a second level heading.
function pageSections() {
    return Array.from(document.querySelectorAll("main section")).filter(
        (section) => section.querySelector(".section-head h2")
    );
}

// An id for each section that has none, made from its heading ("Permanent chat groups" becomes
// "permanent-chat-groups"); the same heading always gives the same id, so links survive a refresh.
function giveSectionsAnchors() {
    const usedIds = new Set();
    for (const section of pageSections()) {
        if (!section.id) {
            const heading = section.querySelector(".section-head h2").textContent;
            let id = heading.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "section";
            // Two sections with the same heading get a number after the second.
            let candidate = id;
            let number = 2;
            while (usedIds.has(candidate) || document.getElementById(candidate)) {
                candidate = `${id}-${number}`;
                number += 1;
            }
            section.id = candidate;
        }
        usedIds.add(section.id);
    }
}

// The bar under the menu with a link to each big section of the page, for long pages; shown only
// when there are at least two sections to jump between.
function setUpSectionBar() {
    const bar = document.getElementById("section-bar");
    if (!bar) {
        return;
    }
    giveSectionsAnchors();
    const sections = pageSections();
    if (sections.length < MIN_SECTIONS_FOR_BAR) {
        return;
    }
    // One link per section, named by its heading without the badges next to it.
    for (const section of sections) {
        const heading = section.querySelector(".section-head h2").cloneNode(true);
        heading.querySelectorAll(".badge").forEach((badge) => badge.remove());
        const link = document.createElement("a");
        link.href = `#${section.id}`;
        link.textContent = heading.textContent.trim();
        bar.appendChild(link);
    }
    bar.hidden = false;
}

setUpThemeToggle();
setUpNavigationMenus();
setUpSortableLists();
setUpConfirmations();
setUpLiveRefresh();
setUpSectionBar();
