/* Watch List preferences and the admin Spotlight chooser. */
(function () {
  "use strict";
  const root = document.getElementById("strategy-overview");
  if (!root) return;

  const catalog = new Set(JSON.parse(document.getElementById("overview-catalog").textContent));
  const templates = new Map(Array.from(root.querySelectorAll("[data-card-template]"),
    template => [template.dataset.cardTemplate, template]));
  const storageKey = "labs.spotlight.v1"; // Preserve existing browser selections across the rename.
  const grid = root.querySelector("[data-watch-grid]");
  const status = root.querySelector("[data-watch-status]");
  const watchManage = root.querySelector("[data-watch-manage]");
  const spotlightManage = root.querySelector("[data-spotlight-manage]");
  const dialog = root.querySelector("#spotlight-chooser");
  let keys = [], scope = "browser", csrf = "", ready = false, saving = false;

  function clean(value) {
    return Array.isArray(value) ? [...new Set(value.filter(k => catalog.has(k)))].slice(0, 100) : [];
  }

  function browserKeys() {
    try { return clean(JSON.parse(localStorage.getItem(storageKey) || "[]")); }
    catch (_) { return []; }
  }

  function render() {
    if (grid) {
      grid.replaceChildren(...keys.map(key => templates.get(key).content.cloneNode(true)));
      root.querySelector("[data-watch-empty]").hidden = !ready || keys.length > 0;
    }
    root.querySelectorAll("[data-watch-toggle]").forEach(button => {
      const selected = keys.includes(button.dataset.watchToggle);
      button.setAttribute("aria-pressed", String(selected));
      button.textContent = selected ? "Remove from Watch List" : "Add to Watch List";
      button.disabled = !ready || saving;
    });
    if (watchManage) watchManage.disabled = !ready || saving;
    const label = root.querySelector("[data-watch-scope]");
    if (label && ready) label.textContent = scope === "account" ? "Saved to your account" :
      scope === "page" ? "Saved for this page only" : "Saved in this browser. Sign in to sync across devices.";
  }

  async function accountRequest(url, next, token) {
    const headers = {Accept: "application/json"};
    const options = {credentials: "same-origin", headers, cache: "no-store"};
    if (next) {
      options.method = "POST";
      options.body = JSON.stringify({keys: next});
      headers["Content-Type"] = "application/json";
      headers["X-CSRF-Token"] = token;
    }
    const response = await fetch(url, options);
    if (!response.ok) {
      const message = response.status === 401 ? "Sign in again to save your selections." :
        response.status === 403 ? "Only administrators can manage Spotlight." : "Could not save selections. Try again.";
      throw Object.assign(new Error(message), {status: response.status});
    }
    const data = await response.json();
    if (!Array.isArray(data.keys) || !data.csrf_token) throw new Error("Invalid preferences response.");
    return data;
  }

  async function saveWatch(next) {
    if (!ready || saving) return false;
    next = clean(next);
    saving = true;
    render();
    try {
      if (scope === "account") {
        const data = await accountRequest(root.dataset.preferencesUrl, next, csrf);
        keys = clean(data.keys);
        csrf = data.csrf_token;
      } else {
        try { localStorage.setItem(storageKey, JSON.stringify(next)); }
        catch (_) { scope = "page"; }
        keys = next;
      }
      status.textContent = "Watch List saved.";
      return true;
    } catch (error) {
      status.textContent = error.message;
      return false;
    } finally {
      saving = false;
      render();
    }
  }

  root.addEventListener("click", event => {
    const button = event.target.closest("[data-watch-toggle]");
    if (!button) return;
    const key = button.dataset.watchToggle;
    saveWatch(keys.includes(key) ? keys.filter(k => k !== key) : keys.concat(key));
  });

  if (dialog) {
    const form = dialog.querySelector("[data-overview-form]");
    const search = dialog.querySelector("[data-overview-search]");
    const inputs = Array.from(form.querySelectorAll('input[name="strategy"]'));
    const submit = form.querySelector('[type="submit"]');
    const feedback = dialog.querySelector("[data-overview-dialog-status]");
    let mode = "watch", picks = [], adminCsrf = "", busy = false;
    const checked = () => inputs.filter(input => input.checked).map(input => input.value);
    const count = () => { dialog.querySelector("[data-overview-count]").textContent = `${checked().length} selected`; };
    const filter = () => {
      const term = search.value.trim().toLocaleLowerCase();
      form.querySelectorAll("[data-choice-group]").forEach(group => {
        const asset = group.querySelector("legend").textContent.toLocaleLowerCase();
        group.querySelectorAll("[data-choice-label]").forEach(label => {
          label.hidden = !`${asset} ${label.textContent}`.toLocaleLowerCase().includes(term);
        });
        group.hidden = !group.querySelector("[data-choice-label]:not([hidden])");
      });
    };
    function open(which, selected) {
      mode = which;
      picks = selected;
      const label = mode === "watch" ? "Watch List" : "Spotlight";
      dialog.querySelector("h2").textContent = `Choose ${label} strategies`;
      submit.textContent = `Save ${label}`;
      inputs.forEach(input => { input.checked = picks.includes(input.value); });
      search.value = "";
      feedback.textContent = "";
      filter(); count(); dialog.showModal();
    }

    watchManage.addEventListener("click", () => open("watch", keys));
    if (spotlightManage) {
      spotlightManage.disabled = false;
      spotlightManage.addEventListener("click", async () => {
        spotlightManage.disabled = true;
        try {
          const data = await accountRequest(root.dataset.spotlightUrl);
          adminCsrf = data.csrf_token;
          if (!dialog.open) open("spotlight", clean(data.keys));
        } catch (error) { status.textContent = error.message; }
        finally { spotlightManage.disabled = false; }
      });
    }
    dialog.querySelector("[data-overview-cancel]").addEventListener("click", () => { if (!busy) dialog.close(); });
    dialog.addEventListener("cancel", event => { if (busy) event.preventDefault(); });
    search.addEventListener("input", filter);
    form.addEventListener("change", count);
    form.addEventListener("submit", async event => {
      event.preventDefault();
      if (busy) return;
      const selected = checked();
      const next = picks.filter(key => selected.includes(key)).concat(selected.filter(key => !picks.includes(key)));
      busy = submit.disabled = true;
      feedback.textContent = "";
      try {
        if (mode === "spotlight") {
          await accountRequest(root.dataset.spotlightUrl, next, adminCsrf);
          window.location.reload();
        } else if (await saveWatch(next)) {
          dialog.close();
        } else {
          feedback.textContent = status.textContent;
        }
      } catch (error) { feedback.textContent = error.message; }
      finally { busy = submit.disabled = false; }
    });
  }

  window.addEventListener("storage", event => {
    if (scope !== "account" && event.key === storageKey && !saving) { keys = browserKeys(); render(); }
  });
  (async function init() {
    try {
      const data = await accountRequest(root.dataset.preferencesUrl);
      if (data.scope !== "account") throw new Error("Invalid preferences response.");
      scope = "account"; keys = clean(data.keys); csrf = data.csrf_token;
    } catch (error) {
      keys = browserKeys();
      if (error.status !== 401) status.textContent = "Account sync is unavailable. Selections will be saved in this browser.";
    }
    ready = true;
    render();
  })();
}());
