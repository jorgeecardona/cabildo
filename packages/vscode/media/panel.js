// Draws the frames of `cabildo panel`. Every text is set as text: agents write parts of it.
// A frame can arrive each second, so elements are updated in place and a click is not lost.
"use strict";

(() => {
  const vscode = acquireVsCodeApi();
  const SVG = "http://www.w3.org/2000/svg";
  const STATES = {
    waiting: "waiting for input",
    working: "working",
    alive: "running",
    "": "stopped",
  };
  const SCOPES = [
    ["here", "Folder", "Folder", "Conversations of the open folder"],
    ["subfolders", "Subfolders", "Sub", "Of the folder and its subfolders"],
    ["all", "All", "All", "Of every directory"],
  ];
  const ACTIONS = [
    ["open", "Open in a terminal", () => true],
    ["message", "Send a message", (item) => item.bus_id],
    ["end", "End the session", (item) => item.host_id],
  ];
  const ICONS = {
    chevron: ["M6 3.5 10.5 8 6 12.5"],
    open: ["M2 3.5h12v9H2z", "M4.5 6.5 6.5 8l-2 1.5", "M8 10h3"],
    message: ["M2 4h12v8.5H2z", "M2.3 4.5 8 8.8l5.7-4.3"],
    end: ["M4.5 4.5l7 7", "M11.5 4.5l-7 7"],
    branch: [
      "M5 5.6v4.8",
      "M11 6.6c0 2.6-6 1.2-6 3.8",
      "M5 2.8a1.4 1.4 0 1 0 .01 0z",
      "M5 10.4a1.4 1.4 0 1 0 .01 0z",
      "M11 3.8a1.4 1.4 0 1 0 .01 0z",
    ],
    note: ["M3 13l.6-2.8 7.2-7.2 2.2 2.2-7.2 7.2z"],
  };
  const PERIODS = { today: "Today", "7d": "7 days", all: "All time", session: "This one" };
  const LEVELS = ["amber", "red"];
  const ROOMS = 2;

  const saved = vscode.getState() ?? {};
  const open = { detail: true, plan: true, ...saved.open };
  let dockHeight = saved.dockHeight ?? 0; // 0: as tall as its content, up to a part of the view
  let frame;
  let selected = "";
  let asked = 0; // when the person last selected: an older frame does not undo it
  let held = false; // a mouse button is down on the list: rows keep their place
  let pending = false;
  let allRooms = false; // the selected conversation shows every room, not the first ROOMS

  function keep() {
    vscode.setState({ open, dockHeight });
  }

  function make(tag, className, parent, value) {
    const node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (value !== undefined) {
      node.textContent = value;
    }
    parent?.append(node);
    return node;
  }

  function icon(name, parent) {
    const svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("class", `icon icon-${name}`);
    svg.setAttribute("aria-hidden", "true");
    for (const d of ICONS[name]) {
      svg.appendChild(document.createElementNS(SVG, "path")).setAttribute("d", d);
    }
    parent?.append(svg);
    return svg;
  }

  function text(node, value) {
    if (node.textContent !== value) {
      node.textContent = value;
    }
  }

  /** `text`, and hidden when there is none. */
  function show(node, value) {
    text(node, value || "");
    node.hidden = !value;
  }

  function level(node, name) {
    for (const known of LEVELS) {
      node.classList.toggle(known, name === known);
    }
  }

  /** Runs `build` only when `data` is not what `node` already shows. */
  function once(node, data, build) {
    const shown = JSON.stringify(data);
    if (node.dataset.shown !== shown) {
      node.dataset.shown = shown;
      node.replaceChildren();
      build(node);
    }
  }

  function actions(parent) {
    const bar = make("div", "actions", parent);
    for (const [act, title] of ACTIONS) {
      const button = make("button", "tool", bar);
      button.type = "button";
      button.dataset.act = act;
      button.title = title;
      button.setAttribute("aria-label", title);
      icon(act, button);
    }
    return bar;
  }

  function allow(bar, item) {
    for (const [act, , allowed] of ACTIONS) {
      bar.querySelector(`[data-act="${act}"]`).hidden = !(item && allowed(item));
    }
  }

  function meter(parent, percent, name, elapsed) {
    const track = make("div", "meter", parent);
    track.setAttribute("role", "meter");
    track.setAttribute("aria-valuemin", "0");
    track.setAttribute("aria-valuemax", "100");
    const fill = make("div", "fill", track);
    const tick = make("div", "tick", track);
    const set = (value, mark, now) => {
      track.setAttribute("aria-valuenow", String(Math.round(value)));
      fill.style.width = `${Math.max(0, Math.min(100, value))}%`;
      level(track, mark);
      tick.hidden = !(now >= 0);
      tick.style.left = `${Math.max(0, Math.min(100, now))}%`;
    };
    set(percent, name, elapsed ?? -1);
    return { track, set };
  }

  function chips(parent, values, className) {
    for (const value of values) {
      make("span", `chip ${className}`, parent, value);
    }
  }

  /** The end of a path: a row has no room for all of it. */
  function tail(dir) {
    const parts = dir.split("/");
    return parts.length > 3 ? `…/${parts.slice(-2).join("/")}` : dir;
  }

  // --- the page --------------------------------------------------------------

  const top = make("header", "top", document.body);
  const scopes = make("div", "segmented", top);
  scopes.setAttribute("role", "radiogroup");
  scopes.setAttribute("aria-label", "Which conversations");
  for (const [scope, label, short, title] of SCOPES) {
    const button = make("button", "", scopes);
    make("span", "long", button, label);
    make("span", "short", button, short); // a narrow view shows this one
    button.setAttribute("aria-label", label);
    button.type = "button";
    button.dataset.scope = scope;
    button.title = title;
    button.setAttribute("role", "radio");
    button.addEventListener("click", () => vscode.postMessage({ type: "scope", scope }));
  }
  const tally = make("div", "tally", top);
  const counts = {};
  for (const mark of ["waiting", "working", "alive"]) {
    const button = make("button", `count ${mark}`, tally);
    button.type = "button";
    make("span", `dot ${mark}`, button);
    counts[mark] = { button, number: make("span", "", button) };
    button.addEventListener("click", () => next(mark));
  }
  const total = make("span", "total", tally);

  const reason = make("p", "reason", document.body);
  reason.hidden = true;
  const list = make("div", "list", document.body);
  list.id = "list";
  list.tabIndex = 0;
  list.setAttribute("role", "listbox");
  list.setAttribute("aria-label", "Conversations");
  const note = make("div", "note", list);
  const sash = make("div", "sash", document.body);
  sash.title = "Drag to resize. Double click: fit the content.";
  const dock = make("div", "dock", document.body);
  const rows = new Map(); // item id -> its element

  // --- the list --------------------------------------------------------------

  function newRow(id) {
    const row = make("div", "row");
    row.dataset.id = id;
    row.setAttribute("role", "option");
    const parts = { dot: make("span", "dot", row) };
    const main = make("div", "main", row);
    const first = make("div", "first", main);
    parts.group = make("span", "chip group", first);
    parts.title = make("span", "title", first);
    parts.age = make("span", "age", first);
    const second = make("div", "second", main);
    parts.agent = make("span", "agent", second);
    parts.branch = make("span", "branch", second);
    icon("branch", parts.branch);
    parts.branchName = make("span", "", parts.branch);
    parts.use = make("span", "use", second);
    parts.dir = make("div", "dir", main);
    parts.extra = make("div", "extra", main);
    parts.actions = actions(row);
    row.parts = parts;
    return row;
  }

  function fill(row, item) {
    const parts = row.parts;
    row.className = `row ${item.mark || "stopped"}`;
    parts.dot.className = `dot ${item.mark}`;
    show(parts.group, item.group);
    text(parts.title, item.title);
    text(parts.age, item.age);
    show(parts.agent, item.agent && `@${item.agent}`);
    text(parts.branchName, item.branch);
    parts.branch.hidden = !item.branch;
    show(parts.dir, item.dir !== frame.folder && tail(item.dir));
    show(parts.use, item.cpu && `${item.cpu} · ${item.mem}`);
    parts.use.title = item.cpu ? `CPU ${item.cpu}, memory ${item.mem}` : "";
    once(parts.extra, [item.tags, item.notes], (extra) => {
      chips(extra, item.tags, "tag");
      for (const value of item.notes) {
        const one = make("span", "noted", extra);
        icon("note", one);
        make("span", "", one, value);
      }
    });
    parts.extra.hidden = !(item.tags.length || item.notes.length);
    row.title = [
      `${item.title} (${STATES[item.mark]})`,
      [item.dir, item.branch].filter(Boolean).join(" · "),
      [item.agent && `@${item.agent}`, ...item.rooms].filter(Boolean).join(" "),
      ...item.notes,
    ]
      .filter(Boolean)
      .join("\n");
    row.setAttribute("aria-label", `${item.title}, ${STATES[item.mark]}, ${item.age}`);
    allow(parts.actions, item);
  }

  function highlight() {
    for (const [id, row] of rows) {
      row.classList.toggle("selected", id === selected);
      row.setAttribute("aria-selected", String(id === selected));
    }
  }

  function drawList() {
    const wanted = new Set(frame.items.map((item) => item.id));
    for (const [id, row] of rows) {
      if (!wanted.has(id)) {
        row.remove();
        rows.delete(id);
      }
    }
    frame.items.forEach((item, index) => {
      let row = rows.get(item.id);
      if (!row) {
        row = newRow(item.id);
        rows.set(item.id, row);
      }
      fill(row, item);
      if (list.children[index] !== row) {
        list.insertBefore(row, list.children[index] ?? null);
      }
    });
    list.append(note);
    const empty = frame.reading ? "Reading the transcripts…" : "No conversation here. + starts one.";
    show(note, frame.items.length ? frame.older && `+${frame.older} older` : empty);
    highlight();
  }

  function drawTop() {
    for (const button of scopes.children) {
      button.setAttribute("aria-checked", String(button.dataset.scope === frame.scope));
    }
    scopes.title = frame.folder;
    for (const [mark, { button, number }] of Object.entries(counts)) {
      const found = frame.items.filter((item) => item.mark === mark).length;
      text(number, String(found));
      button.hidden = !found;
      button.title = `${found} ${STATES[mark]}. Click: the next one.`;
    }
    const all = frame.items.length + frame.older;
    text(total, String(all));
    total.title = `${all} conversation${all === 1 ? "" : "s"}`;
  }

  // --- the sections under the list -------------------------------------------

  /** A section that folds. `summary` is what its heading says while it is folded. */
  function section(key, title) {
    const root = make("section", `fold ${key}`, dock);
    const head = make("div", "head", root);
    const toggle = make("button", "toggle", head);
    toggle.type = "button";
    icon("chevron", toggle);
    const name = make("span", "name", toggle, title);
    const summary = make("span", "summary", toggle);
    const body = make("div", "body", root);
    const apply = () => {
      root.classList.toggle("open", Boolean(open[key]));
      toggle.setAttribute("aria-expanded", String(Boolean(open[key])));
      body.hidden = !open[key];
    };
    toggle.addEventListener("click", () => {
      open[key] = !open[key];
      keep();
      apply();
    });
    apply();
    return { root, head, name, summary, body };
  }

  function stat(parent, label) {
    const cell = make("div", "stat", parent);
    make("div", "label", cell, label);
    return make("div", "value", cell);
  }

  const detail = section("detail", "Conversation");
  const detailActions = actions(detail.head);
  const about = { card: make("div", "card", detail.body) };
  about.title = make("div", "title", about.card);
  about.where = make("div", "where", about.card);
  about.chips = make("div", "chips", about.card);
  about.notes = make("div", "notes", about.card);
  about.context = make("div", "context", about.card);
  about.model = make("span", "model", about.context);
  about.used = make("span", "used", about.context);
  about.percent = make("span", "percent", about.context);
  about.meter = meter(about.card, 0, "");
  about.stats = make("div", "stats", about.card);
  for (const [key, label] of [
    ["tokens_in", "In"],
    ["tokens_out", "Out"],
    ["tokens_cache", "Cache"],
    ["cost", "Cost"],
    ["cpu", "CPU"],
    ["mem", "Memory"],
  ]) {
    about[key] = stat(about.stats, label);
  }

  function drawDetail() {
    const found = frame.detail;
    const item = found && frame.items.find((one) => one.id === frame.selected);
    detail.root.classList.toggle("none", !item);
    allow(detailActions, item);
    detailActions.dataset.id = item ? item.id : "";
    if (!item) {
      once(detail.summary, null, (summary) => {
        summary.textContent = "select one in the list";
      });
      return;
    }
    const context = found.context;
    text(about.title, item.title);
    once(about.where, [found.agent, item.dir, item.branch, item.mark], (where) => {
      const state = make("span", "state", where);
      make("span", `dot ${item.mark}`, state);
      make("span", "", state, STATES[item.mark]);
      if (found.agent) {
        make("span", "agent", where, `@${found.agent}`);
      }
      if (item.branch) {
        const branch = make("span", "branch", where);
        icon("branch", branch);
        make("span", "", branch, item.branch);
      }
      make("span", "dir", where, item.dir);
    });
    const shown = [item.group, item.tags, item.rooms, found.effort, found.upstream, allRooms];
    once(about.chips, shown, (row) => {
      chips(row, item.group ? [item.group] : [], "group");
      chips(row, item.tags, "tag");
      chips(row, found.effort ? [`effort ${found.effort}`] : [], "plain");
      chips(row, found.upstream ? [`via ${found.upstream}`] : [], "plain");
      chips(row, allRooms ? item.rooms : item.rooms.slice(0, ROOMS), "room");
      if (item.rooms.length > ROOMS) {
        const more = make("button", "chip more", row);
        more.type = "button";
        more.textContent = allRooms ? "fewer" : `+${item.rooms.length - ROOMS}`;
        more.title = item.rooms.join(" ");
        more.setAttribute("aria-label", allRooms ? "Show fewer rooms" : "Show every room");
      }
    });
    once(about.notes, item.notes, (notes) => {
      for (const value of item.notes) {
        const one = make("div", "noted", notes);
        icon("note", one);
        make("span", "", one, value);
      }
    });
    about.notes.hidden = !item.notes.length;
    text(about.model, found.model || (context ? "context" : "no reply yet"));
    const window = context?.window ? ` of ${context.window}` : "";
    text(about.used, context ? `${context.used}${window}` : "");
    text(about.percent, context?.window ? `${Math.round(context.percent)}%` : "");
    level(about.percent, context?.level);
    about.meter.track.hidden = !context?.window;
    about.meter.set(context?.percent ?? 0, context?.level ?? "", -1);
    for (const key of ["tokens_in", "tokens_out", "tokens_cache", "cost", "cpu", "mem"]) {
      text(about[key], found[key] || "–");
      about[key].parentElement.hidden = !found[key] && (key === "cost" || key === "tokens_cache");
    }
    once(detail.summary, [found.model, context, found.cost], (summary) => {
      const full = context?.window ? `${Math.round(context.percent)}% full` : "";
      [found.model, full, found.cost].filter(Boolean).forEach((value, index) => {
        const part = make("span", "", summary, `${index ? " · " : ""}${value}`);
        level(part, value === full ? context.level : "");
      });
    });
  }

  const plan = section("plan", "Plan");
  function drawPlan() {
    const found = frame.plan;
    plan.root.hidden = !found?.limits.length;
    if (plan.root.hidden) {
      return;
    }
    text(plan.name, found.name ? `Plan · ${found.name}` : "Plan");
    plan.root.classList.toggle("elsewhere", found.elsewhere);
    once(plan.summary, found.limits.map((l) => [l.name, l.percent, l.level]), (summary) => {
      found.limits.forEach((limit, index) => {
        const value = `${index ? " · " : ""}${limit.name} ${Math.round(limit.percent)}%`;
        level(make("span", "", summary, value), limit.level);
      });
    });
    once(plan.body, found, (body) => {
      for (const limit of found.limits) {
        const row = make("div", "limit", body);
        make("span", "label", row, limit.name);
        const bar = meter(row, limit.percent, limit.level, limit.elapsed);
        bar.track.title =
          limit.elapsed >= 0
            ? `${limit.percent}% used. The mark: ${limit.elapsed}% of the ${limit.name} has passed.`
            : `${limit.percent}% used`;
        level(make("span", "percent", row, `${Math.round(limit.percent)}%`), limit.level);
        const facts = make("div", "facts", row);
        if (limit.pace) {
          make("span", "", facts, limit.pace).title = "Pace: percent per hour";
        }
        if (limit.resets) {
          make("span", "", facts, `resets ${limit.resets}`);
        }
        if (limit.full) {
          const full = make("span", "amber", facts, `full ${limit.full}`);
          full.title = "At this pace it reaches 100% before it resets";
        }
      }
      if (found.elsewhere) {
        make("p", "hint", body, "Another upstream serves the selected conversation.");
      }
    });
  }

  const gateway = section("gateway", "Gateway");
  function drawGateway() {
    const found = frame.gateway;
    gateway.root.hidden = !found.length;
    if (gateway.root.hidden) {
      return;
    }
    const ready = found.filter((up) => up.ready).length;
    text(gateway.summary, `${ready} of ${found.length} ready`);
    level(gateway.summary, ready < found.length ? "amber" : "");
    once(gateway.body, found, (body) => {
      for (const up of found) {
        const row = make("div", `upstream${up.serving ? " serving" : ""}`, body);
        const dot = make("span", `dot ${up.ready ? "ready" : "aside"}`, row);
        dot.title = up.ready ? "ready" : "set aside";
        const name = make("div", "who", row);
        make("span", "name", name, up.name);
        chips(name, up.forced ? ["first"] : [], "plain");
        chips(name, up.serving ? ["this conversation"] : [], "accent");
        if (!up.ready) {
          const why = up.reason ? `: ${up.reason}` : "";
          make("div", "why amber", row, `set aside ${up.set_aside}${why}`);
        }
        const facts = make("div", "facts", row);
        const counts = make("span", "", facts);
        make("span", "", counts, `${up.requests} requests`);
        level(make("span", "", counts, ` · ${up.failovers} failovers`), up.failovers ? "amber" : "");
        counts.title = "Of all sessions, since the daemon started";
        if (up.today || up.week) {
          make("span", "", facts, `today ${up.today}`);
          make("span", "", facts, `7 days ${up.week}`);
        }
      }
    });
  }

  const spend = section("spend", "Spend");
  function drawSpend() {
    const found = frame.spend;
    spend.root.hidden = !found;
    if (!found) {
      return;
    }
    text(spend.name, found.label === "spend" ? "Spend" : `Spend · ${found.label}`);
    text(
      spend.summary,
      found.periods
        .slice(0, 2)
        .map((p) => `${PERIODS[p.name].toLowerCase()} ${p.total}`)
        .join(" · "),
    );
    once(spend.body, found, (body) => {
      const table = make("div", "matrix", body);
      table.style.setProperty("--columns", String(found.periods.length));
      table.setAttribute("role", "table");
      const line = (className, label, values, extra) => {
        const row = make("div", `line ${className}`, table);
        row.setAttribute("role", "row");
        const name = make("div", "name", row, label);
        name.setAttribute("role", "rowheader");
        extra?.(name);
        for (const value of values) {
          const cell = make("div", `cell${value === "-" ? " none" : ""}`, row, value);
          cell.setAttribute("role", "cell");
        }
        return row;
      };
      line("heading", "", found.periods.map((p) => PERIODS[p.name]));
      line("sum", "Total", found.periods.map((p) => p.total)).title =
        "This machine's, from the transcripts, at the rates cabildo knows";
      if (found.billed) {
        line("billed", "Billed", found.periods.map((p) => p.billed)).title =
          "What the organization was billed, on every machine. It lands hours late.";
      }
      if (found.periods.some((p) => p.unpriced)) {
        line("billed", "Unpriced", found.periods.map((p) => (p.unpriced ? `+${p.unpriced}` : ""))).title =
          "Tokens the total leaves out: no price is known for them.";
      }
      for (const model of found.models) {
        const row = line("model", model.name, model.costs, (name) => {
          make("span", "tokens", name, `${model.tokens} tokens`).title = "All time";
          if (model.share >= 0) {
            const share = make("span", "share", name);
            make("span", "fill", share).style.width = `${model.share}%`;
            make("span", "part", name, `${Math.round(model.share)}%`);
          }
        });
        if (model.share >= 0) {
          row.title = `${model.name}: ${model.share}% of the cost of the last 7 days`;
        }
      }
      if (found.models.some((model) => model.share >= 0)) {
        make("p", "hint", body, "Bar and percent: the model's part of the cost of 7 days.");
      }
    });
  }

  const intents = section("intents", "Intents");
  function drawIntents() {
    const found = frame.intents;
    intents.root.hidden = !found;
    if (!found) {
      return;
    }
    const kinds = ["running", "pending"].map((status) => [
      status,
      found.filter((it) => it.status === status).length,
    ]);
    kinds.push(["failed", found.filter((it) => it.kind === "failed").length]);
    once(intents.summary, kinds, (summary) => {
      const some = kinds.filter(([, count]) => count);
      some.forEach(([status, count], index) => {
        const part = make("span", "", summary, `${index ? " · " : ""}${count} ${status}`);
        level(part, status === "failed" ? "red" : "");
      });
      if (!some.length) {
        summary.textContent = found.length ? `${found.length} done` : "none";
      }
    });
    once(intents.body, found, (body) => {
      if (!found.length) {
        make("p", "hint", body, "None in the last hour.");
      }
      for (const it of found) {
        const row = make("div", `intent ${it.kind}`, body);
        make("span", `badge ${it.kind} ${it.status}`, row, it.status);
        const what = make("span", "what", row);
        make("span", "", what, `${it.action} ${it.project}`);
        if (it.by) {
          make("span", "by", what, ` by ${it.by}`);
        }
        make("span", "age", row, it.age);
        if (it.reason) {
          make("div", "why", row, it.reason);
        }
      }
    });
  }

  const runners = section("runners", "Jupyter runners");
  function drawRunners() {
    const found = frame.runners;
    runners.root.hidden = !found.length;
    text(runners.summary, `${found.length} running`);
    once(runners.body, found, (body) => {
      for (const runner of found) {
        const row = make("div", "runner", body);
        make("span", `dot ${runner.paused ? "aside" : "ready"}`, row);
        const name = make("span", "who", row);
        make("span", "name", name, runner.name);
        chips(name, runner.paused ? ["paused"] : [], "plain");
        make("span", "use", row, `${runner.cpu} · ${runner.mem}`).title =
          `CPU ${runner.cpu}, memory ${runner.mem}, with its kernels`;
      }
    });
  }

  const machine = make("div", "machine", dock);
  make("span", "name", machine, "All running");
  machine.title = "Every conversation and runner the panel measures, together";
  const machineUse = make("span", "use", machine);
  function drawMachine() {
    const found = frame.machine;
    machine.hidden = !found;
    text(machineUse, found ? `CPU ${found.cpu} · memory ${found.mem}` : "");
  }

  // --- what the person does --------------------------------------------------

  function select(id, how = "select") {
    selected = id;
    asked = Date.now();
    highlight();
    rows.get(id)?.scrollIntoView({ block: "nearest" });
    vscode.postMessage({ type: how, id });
  }

  /** Select the next conversation in the state `mark`, after the selected one. */
  function next(mark) {
    const items = frame?.items ?? [];
    const at = items.findIndex((item) => item.id === selected);
    const found = [...items.slice(at + 1), ...items.slice(0, at + 1)].find((i) => i.mark === mark);
    if (found) {
      select(found.id, "highlight");
      list.focus();
    }
  }

  list.addEventListener("click", (event) => {
    const row = event.target.closest(".row");
    if (!row) {
      return;
    }
    const act = event.target.closest("button")?.dataset.act;
    if (act) {
      vscode.postMessage({ type: act, id: row.dataset.id });
    } else if (event.detail < 2) {
      select(row.dataset.id); // the second click of a double click does not select again
    }
  });
  list.addEventListener("dblclick", (event) => {
    const row = event.target.closest(".row");
    if (row && !event.target.closest("button")) {
      vscode.postMessage({ type: "open", id: row.dataset.id });
    }
  });
  list.addEventListener("keydown", (event) => {
    const items = frame?.items ?? [];
    const at = items.findIndex((item) => item.id === selected);
    const last = items.length - 1;
    const to = {
      ArrowDown: at + 1,
      j: at + 1,
      ArrowUp: at - 1,
      k: at - 1,
      Home: 0,
      End: last,
    }[event.key];
    if (to !== undefined && items.length) {
      // the arrows only move: Enter opens
      select(items[Math.max(0, Math.min(last, to))].id, "highlight");
    } else if (event.key === "Enter" && at >= 0) {
      vscode.postMessage({ type: "open", id: selected });
    } else {
      return;
    }
    event.preventDefault();
  });
  list.addEventListener("pointerdown", () => {
    held = true;
  });
  for (const type of ["pointerup", "pointercancel"]) {
    window.addEventListener(type, () => {
      held = false;
      if (pending) {
        setTimeout(draw); // after the click this release makes
      }
    });
  }
  about.chips.addEventListener("click", (event) => {
    if (event.target.closest(".more")) {
      allRooms = !allRooms;
      drawDetail();
    }
  });
  detailActions.addEventListener("click", (event) => {
    const act = event.target.closest("button")?.dataset.act;
    if (act && detailActions.dataset.id) {
      vscode.postMessage({ type: act, id: detailActions.dataset.id });
    }
  });

  // The sash sets how tall the sections are; without it they fit their content.
  function size() {
    dock.style.height = dockHeight ? `${dockHeight}px` : "";
    dock.classList.toggle("sized", Boolean(dockHeight));
  }
  sash.addEventListener("pointerdown", (event) => {
    sash.setPointerCapture(event.pointerId);
    const from = event.clientY;
    const start = dock.getBoundingClientRect().height;
    const move = (moved) => {
      const most = document.body.clientHeight - 120;
      dockHeight = Math.round(Math.max(28, Math.min(most, start + from - moved.clientY)));
      size();
    };
    const done = () => {
      sash.removeEventListener("pointermove", move);
      keep();
    };
    sash.addEventListener("pointermove", move);
    sash.addEventListener("pointerup", done, { once: true });
    sash.addEventListener("pointercancel", done, { once: true });
  });
  sash.addEventListener("dblclick", () => {
    dockHeight = 0;
    size();
    keep();
  });
  size();

  function draw() {
    if (!frame) {
      return;
    }
    pending = held;
    if (held) {
      return;
    }
    drawTop();
    drawList();
    drawDetail();
    drawPlan();
    drawGateway();
    drawSpend();
    drawIntents();
    drawRunners();
    drawMachine();
    vscode.postMessage({ type: "rendered", rows: rows.size });
  }

  window.addEventListener("message", ({ data }) => {
    show(reason, data.reason && `No panel: ${data.reason}`);
    if (!data.frame) {
      return;
    }
    const answered = data.frame.selected === selected || Date.now() - asked > 3000;
    if (data.frame.selected !== frame?.selected && answered) {
      selected = data.frame.selected;
    }
    frame = data.frame;
    draw();
  });
  vscode.postMessage({ type: "ready" });
})();
