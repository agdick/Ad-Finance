(function () {
  "use strict";
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";

  async function postJSON(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify(body || {}),
      credentials: "same-origin",
    });
    let data = {};
    try { data = await res.json(); } catch (_) { /* non-JSON error */ }
    if (!res.ok || data.ok === false) throw new Error(data.error || data.detail || `Request failed (${res.status})`);
    return data;
  }

  let toastTimer;
  function toast(message, action) {
    document.querySelector(".toast")?.remove();
    const el = document.createElement("div");
    el.className = "toast";
    const span = document.createElement("span");
    span.textContent = message;
    el.appendChild(span);
    if (action) {
      const btn = document.createElement("button");
      btn.className = "small";
      btn.textContent = action.label;
      btn.addEventListener("click", () => { el.remove(); action.run(); });
      el.appendChild(btn);
    }
    document.body.appendChild(el);
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.remove(), action ? 8000 : 3000);
  }

  // Inline category edit (transactions list). Confirms the category, then offers a rule.
  document.querySelectorAll("select.js-cat").forEach((sel) => {
    sel.addEventListener("change", async () => {
      const id = sel.dataset.id;
      const categoryId = sel.value || null;
      const name = sel.selectedOptions[0]?.textContent.trim();
      try {
        await postJSON(`/api/transactions/${id}/category`, { category_id: categoryId });
        sel.parentElement.querySelector(".pill.warn")?.remove();
        const payee = sel.dataset.payee;
        if (categoryId && payee) {
          toast(`Saved as ${name}.`, {
            label: `Always for “${payee}”`,
            run: async () => {
              try {
                await postJSON(`/api/transactions/${id}/category`, { category_id: categoryId, always: true });
                toast(`Rule created: ${payee} → ${name}`);
              } catch (e) { toast(e.message); }
            },
          });
        } else {
          toast("Saved.");
        }
      } catch (e) {
        toast(e.message);
      }
    });
  });

  // Bank linking. Each provider's browser widget gets a handler keyed by `kind`.
  const linkHandlers = {
    plaid_link: (config, finish) => loadScript("https://cdn.plaid.com/link/v2/stable/link-initialize.js").then(() => {
      const handler = window.Plaid.create({
        token: config.link_token,
        onSuccess: (public_token, metadata) => finish({
          public_token,
          metadata: { institution: metadata.institution || null },
        }),
        onExit: (err) => {
          if (!err) return;
          // Include Plaid's error code (e.g. MFA_NOT_SUPPORTED) so the cause can be looked up.
          const text = err.display_message || err.error_message || "Plaid Link closed.";
          toast(err.error_code ? `${text} (${err.error_code})` : text);
        },
      });
      handler.open();
    }),
  };

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      if (document.querySelector(`script[src="${src}"]`)) return resolve();
      const s = document.createElement("script");
      s.src = src;
      s.onload = resolve;
      s.onerror = () => reject(new Error("Could not load the bank connection widget."));
      document.head.appendChild(s);
    });
  }

  document.querySelectorAll(".js-link").forEach((btn) => {
    btn.addEventListener("click", async (ev) => {
      ev.preventDefault();
      const provider = btn.dataset.provider;
      const connectionId = btn.dataset.connection || null;
      btn.disabled = true;
      try {
        const config = await postJSON("/api/connections/link", { provider, connection_id: connectionId });
        const handler = linkHandlers[config.kind];
        if (!handler) throw new Error(`No browser handler for ${config.kind}`);
        await handler(config, async (data) => {
          toast("Connecting… the first sync can take a minute.");
          try {
            await postJSON("/api/connections/complete", { provider, connection_id: connectionId, data });
          } catch (e) {
            toast(e.message);
            return;
          }
          window.location.reload();
        });
      } catch (e) {
        toast(e.message);
      } finally {
        btn.disabled = false;
      }
    });
  });

  // Goal form: progress source only applies to savings goals.
  document.querySelectorAll(".js-goal-type").forEach((sel) => {
    const form = sel.closest("form");
    const sync = () => {
      const debt = sel.value === "debt";
      const src = form.querySelector(".js-source");
      if (src) src.style.display = debt ? "none" : "";
      const label = form.querySelector(".js-target-label");
      if (label) label.textContent = debt ? "Amount owed at start" : "Target amount";
    };
    sel.addEventListener("change", sync);
    sync();
  });
})();
