/* Personal Spotlight. Account writes are authenticated and CSRF protected. */
(function () {
  "use strict";
  const root = document.getElementById("strategy-overview");
  if (!root) return;
  const catalog = new Set(JSON.parse(document.getElementById("spotlight-catalog").textContent));
  const storageKey = "labs.spotlight.v1";
  const grid = root.querySelector("[data-spotlight-grid]");
  const dialog = root.querySelector("#spotlight-chooser");
  const form = root.querySelector("[data-spotlight-form]");
  const manage = root.querySelector("[data-spotlight-manage]");
  const status = root.querySelector("[data-spotlight-status]");
  let keys = [], scope = "browser", csrf = "", ready = false, saving = false;
  const clean = value => Array.isArray(value) ? [...new Set(value.filter(k => typeof k === "string" && catalog.has(k)))].slice(0, 100) : [];
  function message(text) { status.textContent = text; }
  function scopeLabel() {
    const node = root.querySelector("[data-spotlight-scope]");
    if (node) node.textContent = scope === "account" ? "Your selections are saved to your account." :
      scope === "page" ? "Selections last for this page only; browser storage is unavailable." :
      "Your selections are saved in this browser. Sign in to sync across devices.";
  }
  function browserKeys() {
    try { return clean(JSON.parse(localStorage.getItem(storageKey) || "[]")); }
    catch (_) { return []; }
  }
  function render() {
    if (grid) {
      grid.replaceChildren();
      keys.forEach(key => {
        const template = Array.from(root.querySelectorAll("[data-card-template]")).find(t => t.dataset.cardTemplate === key);
        if (template) grid.append(template.content.cloneNode(true));
      });
      root.querySelector("[data-spotlight-empty]").hidden = keys.length > 0;
    }
    root.querySelectorAll("[data-spotlight-toggle]").forEach(button => {
      const selected = keys.includes(button.dataset.spotlightToggle);
      button.setAttribute("aria-pressed", String(selected));
      button.textContent = selected ? "Remove from Spotlight" : "Add to Spotlight";
      button.disabled = !ready || saving;
    });
    if (manage) manage.disabled = !ready || saving;
    scopeLabel();
  }
  async function save(next) {
    if (!ready || saving) return false;
    next = clean(next);
    saving = true;
    render();
    const submit = form && form.querySelector('[type="submit"]');
    if (submit) submit.disabled = true;
    try {
      if (scope === "account") {
        const response = await fetch(root.dataset.preferencesUrl, {method: "POST", credentials: "same-origin",
          headers: {"Content-Type": "application/json", "Accept": "application/json", "X-CSRF-Token": csrf},
          body: JSON.stringify({keys: next})});
        if (!response.ok) throw new Error(response.status === 401 ? "Your login expired. Sign in again to save Spotlight." : "Could not save Spotlight. Try again.");
        const data = await response.json();
        keys = clean(data.keys);
        csrf = data.csrf_token;
      } else {
        try { localStorage.setItem(storageKey, JSON.stringify(next)); }
        catch (_) { scope = "page"; }
        keys = next;
      }
      message(scope === "account" ? "Spotlight saved to your account." : scope === "page" ? "Spotlight updated for this page." : "Spotlight saved in this browser.");
      return true;
    } catch (error) { message(error.message); return false; }
    finally { saving = false; if (submit) submit.disabled = false; render(); }
  }
  function count() {
    const node = root.querySelector("[data-spotlight-count]");
    if (node) node.textContent = form.querySelectorAll('input[name="strategy"]:checked').length + " selected";
  }
  root.addEventListener("click", async event => {
    const button = event.target.closest("[data-spotlight-toggle]");
    if (button) {
      const key = button.dataset.spotlightToggle;
      await save(keys.includes(key) ? keys.filter(k => k !== key) : keys.concat(key));
    }
  });
  if (dialog) {
    manage.addEventListener("click", () => {
      form.querySelectorAll('input[name="strategy"]').forEach(input => { input.checked = keys.includes(input.value); });
      root.querySelector("[data-spotlight-search]").value = "";
      form.querySelectorAll("[data-choice-label], [data-choice-group]").forEach(node => { node.hidden = false; });
      count(); dialog.showModal();
    });
    root.querySelector("[data-spotlight-cancel]").addEventListener("click", () => { if (!saving) dialog.close(); });
    dialog.addEventListener("cancel", event => { if (saving) event.preventDefault(); });
    form.addEventListener("change", count);
    form.addEventListener("submit", async event => {
      event.preventDefault();
      // Keep existing Spotlight order; append newly selected strategies in catalog order.
      const checked = Array.from(form.querySelectorAll('input[name="strategy"]:checked')).map(input => input.value);
      const next = keys.filter(key => checked.includes(key)).concat(checked.filter(key => !keys.includes(key)));
      if (await save(next)) dialog.close();
    });
    root.querySelector("[data-spotlight-search]").addEventListener("input", event => {
      const term = event.target.value.trim().toLocaleLowerCase();
      form.querySelectorAll("[data-choice-group]").forEach(group => {
        group.querySelectorAll("[data-choice-label]").forEach(label => { label.hidden = !label.textContent.toLocaleLowerCase().includes(term); });
        group.hidden = !Array.from(group.querySelectorAll("[data-choice-label]")).some(label => !label.hidden);
      });
    });
  }
  window.addEventListener("storage", event => {
    if (scope !== "account" && event.key === storageKey && !saving) { keys = browserKeys(); render(); }
  });
  (async function init() {
    try {
      const response = await fetch(root.dataset.preferencesUrl, {credentials: "same-origin", headers: {Accept: "application/json"}, cache: "no-store"});
      if (response.ok) {
        const data = await response.json();
        if (data.scope !== "account" || !data.csrf_token) throw new Error("Invalid preferences response");
        scope = "account"; keys = clean(data.keys); csrf = data.csrf_token;
      } else if (response.status === 401) keys = browserKeys();
      else throw new Error("Account preferences unavailable");
    } catch (_) { keys = browserKeys(); message("Account sync is unavailable. Selections will be saved in this browser."); }
    ready = true; render();
  })();
}());
