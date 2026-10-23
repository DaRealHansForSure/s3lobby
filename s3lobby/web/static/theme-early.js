// Applies the saved theme before the page paints, so it does not flash in the other theme. Loaded
// as a file, not inline, so the Content-Security-Policy can forbid inline scripts.
try {
    var savedTheme = localStorage.getItem("theme");
    if (savedTheme === "light" || savedTheme === "dark") {
        document.documentElement.setAttribute("data-theme", savedTheme);
    }
} catch (error) {
    // Storage can be blocked; the system preference still applies.
}
