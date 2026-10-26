// Apply the saved palette before the first paint, including on the TV display.
(() => {
  const themes = ["sunset", "midnight", "daylight"];
  function apply(value) {
    const theme = themes.includes(value) ? value : "sunset";
    document.documentElement.dataset.theme = theme;
    document.querySelectorAll("[data-theme-picker]").forEach(el => { el.value = theme; });
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = { sunset: "#211820", midnight: "#111c2b", daylight: "#faf5ef" }[theme];
  }
  try { apply(localStorage.getItem("kq:theme")); } catch (_) { apply("sunset"); }
  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-theme-picker]").forEach(el => {
      el.value = document.documentElement.dataset.theme;
      el.addEventListener("change", () => {
        apply(el.value);
        try { localStorage.setItem("kq:theme", el.value); } catch (_) { /* Works for this visit too. */ }
      });
    });
  });
  addEventListener("storage", event => { if (event.key === "kq:theme") apply(event.newValue); });
})();
