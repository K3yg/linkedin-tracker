// Optimistic job actions (Applied / Dismiss / Block / Trust / Undo): the card leaves at once and
// the request runs in the background, so clicks never wait for each other. A failed request puts
// the card back. Tab counts update locally and are re-synced with the server once things settle.
(() => {
  const view = document.body.dataset.view;
  const gone = new Set();          // job ids acted on — hidden even if a stale list refresh brings them back
  const goneCompanies = new Set(); // blocked / trusted companies (all their cards leave this view)
  let inflight = 0;
  let syncTimer;

  const isGone = (card) => gone.has(card.id) || goneCompanies.has(card.dataset.company);

  function bumpCount(key, delta) {
    const el = document.querySelector(`#tabs [data-view="${key}"] .count`);
    if (el) el.textContent = Math.max(0, (parseInt(el.textContent, 10) || 0) + delta);
  }

  function refreshListChrome() {
    const head = document.querySelector("#list .group-head span");
    if (head) head.textContent = document.querySelectorAll("#list .job.row:not(.leaving)").length;
    const picks = document.querySelector("#list .picks");
    if (picks && !picks.querySelector(".job:not(.leaving)")) picks.hidden = true;
  }

  function leave(cards) {
    for (const card of cards) {
      card.classList.add("leaving");
      setTimeout(() => { if (card.classList.contains("leaving")) card.hidden = true; }, 200);
    }
    refreshListChrome();
  }

  function restore(cards) {
    for (const card of cards) {
      card.classList.remove("leaving");
      card.hidden = false;
    }
    const picks = document.querySelector("#list .picks");
    if (picks) picks.hidden = false;
    refreshListChrome();
  }

  function toast(text) {
    const el = document.createElement("div");
    el.className = "toast";
    el.textContent = text;
    document.body.append(el);
    setTimeout(() => el.remove(), 4000);
  }

  function scheduleSync() {
    clearTimeout(syncTimer);
    syncTimer = setTimeout(() => { if (!inflight) htmx.trigger(document.body, "countsChanged"); }, 600);
  }

  document.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    e.preventDefault();
    const card = btn.closest(".job");
    if (!card || card.classList.contains("leaving")) return;
    if (btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;

    const company = card.dataset.company;
    const wholeCompany = btn.hasAttribute("data-whole-company");
    const cards = wholeCompany
      ? [...document.querySelectorAll("#list .job")].filter((c) => c.dataset.company === company && !c.classList.contains("leaving"))
      : [card];
    const ids = cards.map((c) => c.id);
    const to = btn.dataset.to;
    const jobs = cards.reduce((n, c) => n + (parseInt(c.dataset.jobs, 10) || 1), 0); // incl. copies in other cities

    if (wholeCompany) goneCompanies.add(company);
    ids.forEach((id) => gone.add(id));
    leave(cards);
    bumpCount(view, -jobs);
    if (to) bumpCount(to, jobs);

    inflight++;
    try {
      const body = new URLSearchParams(to ? { to } : { company });
      const res = await fetch(btn.dataset.act, { method: "POST", body });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      cards.forEach((c) => c.remove());
    } catch (err) {
      if (wholeCompany) goneCompanies.delete(company);
      ids.forEach((id) => gone.delete(id));
      bumpCount(view, jobs);
      if (to) bumpCount(to, -jobs);
      if (cards.every((c) => c.isConnected)) restore(cards);
      else htmx.trigger(document.body, "listStale"); // the list was re-rendered meanwhile
      toast(`Couldn't save that (${err.message}) — the job is back in the list.`);
    } finally {
      inflight--;
      scheduleSync();
    }
  });

  // Don't re-render the list (closing it) while a "What changed" panel is open; searches still run.
  document.addEventListener("htmx:beforeRequest", (e) => {
    if (e.detail.elt.id === "list" && document.querySelector("#list details[open]")) e.preventDefault();
  });

  // Periodic / search refreshes can return jobs whose request hasn't landed yet: drop them.
  document.addEventListener("htmx:afterSwap", (e) => {
    if (e.detail.target.id !== "list") return;
    document.querySelectorAll("#list .job").forEach((c) => { if (isGone(c)) c.remove(); });
    refreshListChrome();
  });
})();
