(function () {
  const RANK = {
    operational: 0,
    degraded: 1,
    partial_outage: 2,
    major_outage: 3,
  };

  const LABEL = {
    operational: "All systems operational",
    degraded: "Degraded performance",
    partial_outage: "Partial outage",
    major_outage: "Major outage",
  };

  function worstStatus(statuses) {
    let worst = "operational";
    for (const status of statuses) {
      if ((RANK[status] || 0) > (RANK[worst] || 0)) {
        worst = status;
      }
    }
    return worst;
  }

  function formatWhen(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toISOString().replace("T", " ").replace(/\.\d+Z$/, " UTC");
  }

  function badge(status) {
    const el = document.createElement("span");
    el.className = "badge " + (status || "operational");
    el.textContent = String(status || "operational").replaceAll("_", " ");
    return el;
  }

  function renderComponents(components) {
    const list = document.getElementById("components");
    list.replaceChildren();
    for (const component of components) {
      const li = document.createElement("li");
      const left = document.createElement("div");
      const name = document.createElement("div");
      name.className = "name";
      name.textContent = component.name;
      left.appendChild(name);
      if (component.description) {
        const desc = document.createElement("p");
        desc.className = "desc";
        desc.textContent = component.description;
        left.appendChild(desc);
      }
      li.appendChild(left);
      li.appendChild(badge(component.status));
      list.appendChild(li);
    }
  }

  function renderIncidents(incidents) {
    const root = document.getElementById("incidents");
    root.replaceChildren();
    if (!incidents || incidents.length === 0) {
      const p = document.createElement("p");
      p.className = "empty";
      p.textContent = "No open or recent incidents.";
      root.appendChild(p);
      return;
    }
    for (const incident of incidents) {
      const card = document.createElement("article");
      card.className = "card";
      const h = document.createElement("h3");
      h.textContent = incident.title || "Incident";
      card.appendChild(h);
      const when = document.createElement("p");
      when.className = "when";
      const parts = [
        incident.status ? String(incident.status).replaceAll("_", " ") : null,
        incident.started_at ? "started " + formatWhen(incident.started_at) : null,
        incident.resolved_at ? "resolved " + formatWhen(incident.resolved_at) : null,
      ].filter(Boolean);
      when.textContent = parts.join(" · ");
      card.appendChild(when);
      if (incident.summary) {
        const summary = document.createElement("p");
        summary.textContent = incident.summary;
        card.appendChild(summary);
      }
      if (Array.isArray(incident.updates)) {
        for (const update of incident.updates) {
          const line = document.createElement("p");
          const stamp = update.at ? formatWhen(update.at) + " — " : "";
          line.textContent = stamp + (update.body || "");
          card.appendChild(line);
        }
      }
      root.appendChild(card);
    }
  }

  function renderSignals(signals) {
    const root = document.getElementById("signals");
    root.replaceChildren();
    const active = (signals || []).filter((s) => s.state === "ALARM");
    if (active.length === 0) {
      const p = document.createElement("p");
      p.className = "empty";
      p.textContent = "No active automated signals.";
      root.appendChild(p);
      return;
    }
    for (const signal of active) {
      const card = document.createElement("article");
      card.className = "card";
      const h = document.createElement("h3");
      h.textContent = signal.component_name || signal.component || "Signal";
      card.appendChild(h);
      const when = document.createElement("p");
      when.className = "when";
      when.textContent =
        (signal.alarm_name || "alarm") +
        " · ALARM" +
        (signal.since ? " since " + formatWhen(signal.since) : "");
      card.appendChild(when);
      if (signal.summary) {
        const summary = document.createElement("p");
        summary.textContent = signal.summary;
        card.appendChild(summary);
      }
      root.appendChild(card);
    }
  }

  function applyComponentSignals(data) {
    const components = (data.components || []).map((c) => ({ ...c }));
    const byId = Object.fromEntries(components.map((c) => [c.id, c]));
    for (const signal of data.signals || []) {
      if (signal.state !== "ALARM") continue;
      const target = byId[signal.component];
      if (!target) continue;
      const next = signal.severity || "degraded";
      if ((RANK[next] || 0) > (RANK[target.status] || 0)) {
        target.status = next;
      }
    }
    // Manual incident open status can also raise components.
    for (const incident of data.incidents || []) {
      if (incident.resolved_at) continue;
      const impact = incident.impact || "degraded";
      for (const id of incident.components || []) {
        const target = byId[id];
        if (!target) continue;
        if ((RANK[impact] || 0) > (RANK[target.status] || 0)) {
          target.status = impact;
        }
      }
    }
    return components;
  }

  fetch("./status.json", { cache: "no-store" })
    .then((resp) => {
      if (!resp.ok) throw new Error("status.json HTTP " + resp.status);
      return resp.json();
    })
    .then((data) => {
      const components = applyComponentSignals(data);
      const overall = worstStatus(components.map((c) => c.status));
      document.getElementById("overall-title").textContent =
        LABEL[overall] || "Status";
      document.getElementById("overall-blurb").textContent =
        data.description || "";
      document.getElementById("updated").textContent = data.updated_at
        ? "Last updated " + formatWhen(data.updated_at)
        : "";
      document.getElementById("history-note").textContent =
        data.history_note || "";
      renderComponents(components);
      renderIncidents(data.incidents || []);
      renderSignals(data.signals || []);
      document.title =
        (overall === "operational" ? "Operational" : "Status") +
        " · TerranPage";
    })
    .catch((err) => {
      document.getElementById("overall-title").textContent =
        "Status temporarily unavailable";
      document.getElementById("overall-blurb").textContent =
        "Could not load status.json from this host. The product site may still be up.";
      document.getElementById("updated").textContent = String(err);
    });
})();
