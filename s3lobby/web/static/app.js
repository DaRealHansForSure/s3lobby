// The theme toggle, and the live lobby's refresh. Every page works without this script; it only
// adds the toggle and keeps the live lobby current without reloading.

const LIVE_REFRESH_INTERVAL_MS = 3000;

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
            }
        } catch (error) {
            // The web interface is unreachable for a moment; the next refresh tries again.
        }
    }

    window.setInterval(refreshLiveState, LIVE_REFRESH_INTERVAL_MS);
    document.addEventListener("visibilitychange", refreshLiveState);
}

setUpThemeToggle();
setUpLiveRefresh();
