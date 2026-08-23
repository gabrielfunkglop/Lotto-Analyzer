// Theme: remember an explicit choice, otherwise follow the OS.
(function () {
  var saved = null;
  try { saved = localStorage.getItem("nlcb-theme"); } catch (e) {}
  if (saved === "dark" || saved === "light") {
    document.documentElement.setAttribute("data-theme", saved);
  }
  document.addEventListener("click", function (ev) {
    var t = ev.target.closest("#themeToggle");
    if (!t) return;
    var cur = document.documentElement.getAttribute("data-theme");
    if (!cur) {
      cur = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }
    var next = cur === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("nlcb-theme", next); } catch (e) {}
  });
})();

// Games dropdown
document.addEventListener("click", function (ev) {
  var btn = ev.target.closest(".dropdown > button");
  document.querySelectorAll(".dropdown").forEach(function (d) {
    if (btn && d.contains(btn)) d.classList.toggle("open");
    else d.classList.remove("open");
  });
});
document.addEventListener("keydown", function (ev) {
  if (ev.key === "Escape") {
    document.querySelectorAll(".dropdown.open").forEach(function (d) { d.classList.remove("open"); });
  }
});

// Client-side filter for any table marked data-filterable, driven by an input
// whose data-filter attribute names the table id.
document.addEventListener("input", function (ev) {
  var input = ev.target.closest("[data-filter]");
  if (!input) return;
  var table = document.getElementById(input.getAttribute("data-filter"));
  if (!table) return;
  var q = input.value.trim().toLowerCase();
  var shown = 0;
  table.querySelectorAll("tbody tr").forEach(function (tr) {
    var hit = !q || tr.textContent.toLowerCase().indexOf(q) !== -1;
    tr.hidden = !hit;
    if (hit) shown++;
  });
  var out = document.querySelector('[data-filter-count="' + input.getAttribute("data-filter") + '"]');
  if (out) out.textContent = shown + " shown";
});
