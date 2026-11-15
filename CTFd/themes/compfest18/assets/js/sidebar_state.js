"use strict";

(() => {
  const STORAGE_KEY = "sidebar-collapsed";

  function getSidebarState() {
    return localStorage.getItem(STORAGE_KEY) === "true";
  }

  let sidebarCollapsed = getSidebarState();

  if (sidebarCollapsed) {
    document.documentElement.classList.add("sidebar-collapsed");
  }

  window.addEventListener("load", () => {
    const toggle = document.getElementById("sidebarCollapseToggle");
    if (!toggle) return;

    toggle.addEventListener("click", ev => {
      sidebarCollapsed = !sidebarCollapsed;

      document.documentElement.classList.toggle("sidebar-collapsed", sidebarCollapsed);

      localStorage.setItem(STORAGE_KEY, sidebarCollapsed);
      toggle.setAttribute("aria-expanded", String(!sidebarCollapsed));
      ev.preventDefault();
    });
  });
})();
