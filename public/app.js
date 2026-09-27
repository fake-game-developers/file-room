const slotsEl = document.querySelector("#slots");
const recordsEl = document.querySelector("#records");
const statusEl = document.querySelector("#status");
const bannerEl = document.querySelector("#banner");
const filterEl = document.querySelector("#filter");

let state = null;
let openForm = null;
let confirmRestore = null;

document.querySelector("#refresh").addEventListener("click", () => load());
filterEl.addEventListener("input", () => renderRecords());

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function money(value) {
  if (value == null) return "—";
  return Number(value).toLocaleString("en-US");
}

function when(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function planet(stats) {
  if (!stats || stats.planetId == null) return "";
  return stats.planetName ? `${stats.planetName} · #${stats.planetId}` : `Moon #${stats.planetId}`;
}

function summary(stats) {
  if (!stats || stats.missing) return "No save in this slot.";
  if (stats.unreadable) return "Save is there, but it could not be read. It can still be archived as-is.";
  if (stats.empty) return "Vacant. The game has a blank file here.";
  const moon = planet(stats);
  return [
    `${money(stats.fulfilled)} / ${money(stats.quota)} quota`,
    stats.daysLeft == null ? null : `${stats.daysLeft} days left`,
    moon || null,
    `${stats.quotasPassed ?? 0} quotas met`,
    `${stats.daysSpent ?? 0} days spent`,
    `${stats.deaths ?? 0} deaths`,
  ]
    .filter(Boolean)
    .join(" · ");
}

function quotaWidth(stats) {
  if (!stats || !stats.quota) return 0;
  return Math.max(0, Math.min(100, (stats.fulfilled / stats.quota) * 100));
}

function showError(message) {
  bannerEl.hidden = !message;
  bannerEl.textContent = message || "";
}

window.fileRoom = {
  async call(method, url, payload) {
    if (!window.pywebview || !window.pywebview.api) {
      throw new Error("Open File Room from the desktop app.");
    }
    const result = await window.pywebview.api.dispatch(method, url, payload || {});
    if (!result || !result.ok) throw new Error((result && result.error) || "Request failed.");
    return result.data;
  },
};

async function api(url, options) {
  const method = (options && options.method) || "GET";
  const payload = options && options.body ? JSON.parse(options.body) : {};
  return window.fileRoom.call(method, url, payload);
}

async function load() {
  try {
    state = await api("/api/state");
    const notices = [];
    if (state.saveDirMissing) {
      notices.push("The Lethal Company save folder was not found at " + state.saveDir + ".");
    }
    if (state.gameRunning) {
      notices.push("Lethal Company is open. Close it before archiving or restoring, or the game will write over the change when it quits.");
    }
    showError(notices.join(" "));
    if (openForm) {
      renderStatus();
      return;
    }
    render();
  } catch (error) {
    showError(error.message);
  }
}

function render() {
  const typing = document.activeElement && /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName);
  if (typing && document.activeElement.id === "filter") {
    renderStatus();
    renderSlots();
    return;
  }
  if (typing) return;
  renderStatus();
  renderSlots();
  renderRecords();
}

function renderStatus() {
  const game = state.gameRunning
    ? `<span class="pill warn"><i></i> Ship is running</span>`
    : `<span class="pill"><i></i> Ship is docked</span>`;
  statusEl.innerHTML = `
    ${game}
    <span>${state.archives.length} archived</span>
    <span title="${esc(state.databasePath)}">Database · ${esc(state.databasePath)}</span>
    <span title="${esc(state.saveDir)}">Saves · ${esc(state.saveDir)}</span>
    <span>v${esc(state.version || "")}</span>
    <button type="button" id="open-library">Open archive folder</button>
    <button type="button" id="check-update">Check for updates</button>
  `;
  document.querySelector("#open-library").addEventListener("click", () => {
    api("/api/open-library", { method: "POST", body: "{}" }).catch((error) => showError(error.message));
  });
  document.querySelector("#check-update").addEventListener("click", () => checkUpdates());
}

async function checkUpdates() {
  const button = document.querySelector("#check-update");
  if (button) {
    button.disabled = true;
    button.textContent = "Checking";
  }
  const notice = document.querySelector("#update");
  try {
    const info = await api("/api/update");
    notice.hidden = false;
    const action = info.canInstall
      ? `<button type="button" class="solid" id="install-update">Install and restart</button>`
      : info.updateAvailable
        ? `<button type="button" class="ghost" id="open-release">Open release</button>`
        : "";
    notice.innerHTML = `<span>${esc(info.message)}</span>${action}`;
    const install = document.querySelector("#install-update");
    if (install) install.addEventListener("click", () => installUpdate(info));
    const openRelease = document.querySelector("#open-release");
    if (openRelease) {
      openRelease.addEventListener("click", () => {
        api("/api/open-url", { method: "POST", body: JSON.stringify({ url: info.releaseUrl }) }).catch((error) => showError(error.message));
      });
    }
  } catch (error) {
    showError(error.message);
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "Check for updates";
    }
  }
}

async function installUpdate() {
  const button = document.querySelector("#install-update");
  if (button) {
    button.disabled = true;
    button.textContent = "Installing";
  }
  try {
    await api("/api/update", { method: "POST", body: "{}" });
  } catch (error) {
    showError(error.message);
    if (button) {
      button.disabled = false;
      button.textContent = "Install and restart";
    }
  }
}

function renderSlots() {
  const locked = state.gameRunning;
  slotsEl.innerHTML = state.slots
    .map((slot) => {
      const stats = slot.stats;
      const headline = stats.empty || stats.missing
        ? `<p class="credits">Vacant</p>`
        : `<p class="credits">${esc(money(stats.credits))}<span>CR</span></p>`;
      const stamp = stats.empty || stats.missing ? `<span class="stamp">Vacant</span>` : "";
      const bar = stats.empty || stats.missing || stats.unreadable
        ? ""
        : `<div class="bar" aria-hidden="true"><span style="width:${quotaWidth(stats)}%"></span></div>`;
      const form = openForm === `slot-${slot.slot}`
        ? `<form class="form" data-archive="${slot.slot}">
            <label>Alias <input name="alias" type="text" maxlength="48" required value="${esc(suggestedAlias(slot))}" /></label>
            <label>Notes <textarea name="notes" maxlength="400" placeholder="Crew, moon plan, why you kept it"></textarea></label>
            <div class="actions">
              <button class="solid" type="submit">File it</button>
              <button class="ghost" type="button" data-close>Cancel</button>
            </div>
          </form>`
        : "";
      return `<article class="drawer">
        <p class="ghost">${slot.slot}</p>
        <div class="drawer-top">
          <p class="kicker">File ${slot.slot}</p>
          ${stamp}
        </div>
        ${headline}
        <p class="meta">${esc(summary(stats))}</p>
        ${slot.modifiedAt && !stats.missing ? `<p class="quiet">Written ${esc(when(slot.modifiedAt))}</p>` : ""}
        ${bar}
        <div class="actions">
          <button class="solid" type="button" data-open="slot-${slot.slot}" ${locked || stats.missing ? "disabled" : ""}>Archive</button>
        </div>
        ${form}
      </article>`;
    })
    .join("");

  slotsEl.querySelectorAll("[data-open]").forEach((button) => {
    button.addEventListener("click", () => {
      openForm = button.dataset.open;
      confirmRestore = null;
      renderSlots();
      const input = slotsEl.querySelector("input[name=alias]");
      if (input) input.focus();
    });
  });
  slotsEl.querySelectorAll("[data-close]").forEach((button) => {
    button.addEventListener("click", () => {
      openForm = null;
      renderSlots();
    });
  });
  slotsEl.querySelectorAll("form[data-archive]").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = new FormData(form);
      try {
        await api("/api/archive", {
          method: "POST",
          body: JSON.stringify({
            slot: Number(form.dataset.archive),
            alias: data.get("alias"),
            notes: data.get("notes"),
          }),
        });
        openForm = null;
        await load();
      } catch (error) {
        showError(error.message);
      }
    });
  });
}

function suggestedAlias(slot) {
  const stats = slot.stats;
  if (!stats || stats.empty || stats.missing || stats.credits == null) return `File ${slot.slot}`;
  const moon = stats.planetName ? ` ${stats.planetName}` : "";
  return `File ${slot.slot} · ${money(stats.credits)} cr${moon}`.slice(0, 48);
}

function filteredArchives() {
  const query = filterEl.value.trim().toLowerCase();
  if (!query) return state.archives;
  return state.archives.filter((record) =>
    `${record.alias} ${record.notes}`.toLowerCase().includes(query)
  );
}

function renderRecords() {
  const locked = state.gameRunning;
  const records = filteredArchives();
  if (!state.archives.length) {
    recordsEl.innerHTML = `<p class="empty-archive">No records filed. Archive a file on the left and it will wait here, under whatever name you give it.</p>`;
    return;
  }
  if (!records.length) {
    recordsEl.innerHTML = `<p class="empty-archive">Nothing in the archive matches that search.</p>`;
    return;
  }
  recordsEl.innerHTML = records
    .map((record, index) => {
      const stats = record.stats || {};
      const line = stats.unreadable
        ? "Unreadable save. Restore still copies the original bytes."
        : stats.empty
          ? "Blank file."
          : `${money(stats.credits)} cr · ${money(stats.fulfilled)}/${money(stats.quota)} · ${planet(stats) || "moon unknown"}`;
      const editing = openForm === `edit-${record.id}`;
      const restoring = openForm === `restore-${record.id}`;
      const editor = editing
        ? `<form class="form" data-edit="${record.id}">
            <label>Alias <input name="alias" type="text" maxlength="48" required value="${esc(record.alias)}" /></label>
            <label>Notes <textarea name="notes" maxlength="400">${esc(record.notes)}</textarea></label>
            <div class="actions">
              <button class="solid" type="submit">Save</button>
              <button class="ghost" type="button" data-close>Cancel</button>
            </div>
          </form>`
        : "";
      const restore = restoring
        ? `<div class="restore-row">
            ${[1, 2, 3]
              .map((slot) => {
                const target = state.slots.find((item) => item.slot === slot);
                const needsConfirm = target && target.occupied;
                const armed = confirmRestore === `${record.id}:${slot}`;
                const label = armed ? `Confirm file ${slot}` : needsConfirm ? `Overwrite ${slot}` : `File ${slot}`;
                return `<button class="${armed ? "danger" : "ghost"}" type="button" data-restore="${record.id}" data-slot="${slot}" ${locked ? "disabled" : ""}>${label}</button>`;
              })
              .join("")}
          </div>`
        : "";
      return `<article class="folder" style="animation-delay:${index * 40}ms">
        <header>
          <p class="when">From file ${record.sourceSlot}</p>
          <p class="when">${esc(when(record.createdAt))}</p>
        </header>
        <h3>${esc(record.alias)}</h3>
        <p class="figures">${esc(line)}</p>
        ${record.notes ? `<p class="notes">${esc(record.notes)}</p>` : ""}
        ${editor}
        <div class="actions">
          <button class="solid" type="button" data-open="restore-${record.id}" ${locked || record.missing ? "disabled" : ""}>Restore</button>
          <button class="ghost" type="button" data-open="edit-${record.id}">Edit</button>
          <button class="danger" type="button" data-delete="${record.id}">${confirmRestore === `delete:${record.id}` ? "Confirm discard" : "Discard"}</button>
        </div>
        ${restore}
      </article>`;
    })
    .join("");

  recordsEl.querySelectorAll("[data-open]").forEach((button) => {
    button.addEventListener("click", () => {
      openForm = openForm === button.dataset.open ? null : button.dataset.open;
      confirmRestore = null;
      renderRecords();
    });
  });
  recordsEl.querySelectorAll("[data-close]").forEach((button) => {
    button.addEventListener("click", () => {
      openForm = null;
      renderRecords();
    });
  });
  recordsEl.querySelectorAll("form[data-edit]").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = new FormData(form);
      try {
        await api(`/api/archive/${form.dataset.edit}`, {
          method: "PATCH",
          body: JSON.stringify({ alias: data.get("alias"), notes: data.get("notes") }),
        });
        openForm = null;
        await load();
      } catch (error) {
        showError(error.message);
      }
    });
  });
  recordsEl.querySelectorAll("[data-restore]").forEach((button) => {
    button.addEventListener("click", async () => {
      const key = `${button.dataset.restore}:${button.dataset.slot}`;
      const target = state.slots.find((item) => item.slot === Number(button.dataset.slot));
      if (target && target.occupied && confirmRestore !== key) {
        confirmRestore = key;
        renderRecords();
        return;
      }
      try {
        await api("/api/restore", {
          method: "POST",
          body: JSON.stringify({
            id: button.dataset.restore,
            slot: Number(button.dataset.slot),
          }),
        });
        openForm = null;
        confirmRestore = null;
        await load();
      } catch (error) {
        showError(error.message);
      }
    });
  });
  recordsEl.querySelectorAll("[data-delete]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (confirmRestore !== `delete:${button.dataset.delete}`) {
        confirmRestore = `delete:${button.dataset.delete}`;
        renderRecords();
        return;
      }
      try {
        await api(`/api/archive/${button.dataset.delete}`, { method: "DELETE" });
        confirmRestore = null;
        await load();
      } catch (error) {
        showError(error.message);
      }
    });
  });
}

function boot() {
  load();
  setInterval(() => {
    const typing = document.activeElement && /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName);
    if (!typing) load();
  }, 8000);
}

if (window.pywebview && window.pywebview.api) boot();
else window.addEventListener("pywebviewready", boot, { once: true });
