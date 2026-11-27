// The console view: one xterm.js terminal per tab. The extension holds the connections; this page
// draws what it sends and sends back keys and sizes. Every text is set as text, never as HTML.
(() => {
  const vscode = acquireVsCodeApi();
  const post = (message) => vscode.postMessage(message);

  const ANSI = [
    "Black",
    "Red",
    "Green",
    "Yellow",
    "Blue",
    "Magenta",
    "Cyan",
    "White",
  ];

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

  function button(className, parent, label, title, run) {
    const node = make("button", className, parent, label);
    node.type = "button";
    node.title = title;
    node.setAttribute("aria-label", title);
    node.addEventListener("click", (event) => {
      event.stopPropagation();
      run();
    });
    return node;
  }

  const bar = make("div", "bar", document.body);
  const strip = make("div", "tabs", bar);
  strip.setAttribute("role", "tablist");
  button("tool new", bar, "+", "New Claude session in the folder", () => post({ type: "new" }));
  const about = make("div", "about", bar);
  const model = make("span", "model", about);
  const context = make("span", "context", about);
  const meter = make("span", "meter", context);
  const fill = make("span", "fill", meter);
  const used = make("span", "", context);
  const cost = make("span", "cost", about);
  const end = button("end", about, "End", "End the session: Claude stops", () =>
    post({ type: "end", id: active }),
  );

  const stage = make("div", "stage", document.body);
  const empty = make("div", "empty", stage);
  make("p", "", empty, "No session here.");
  make(
    "p",
    "",
    empty,
    "Click a conversation that the session host runs, or start one with the + button.",
  );

  const terminals = new Map(); // id -> { box, term, fit, size }
  let tabs = [];
  let active = "";
  let font = { family: "monospace", size: 14 };

  // --- the terminals ---------------------------------------------------------

  function theme() {
    const style = getComputedStyle(document.documentElement);
    const color = (...names) => {
      for (const name of names) {
        const value = style.getPropertyValue(`--vscode-${name}`).trim();
        if (value) {
          return value;
        }
      }
      return undefined;
    };
    const background = color(
      "terminal-background",
      "panel-background",
      "sideBar-background",
      "editor-background",
    );
    const foreground = color("terminal-foreground", "foreground");
    const found = {
      background,
      foreground,
      cursor: color("terminalCursor-foreground") ?? foreground,
      cursorAccent: background,
      selectionBackground: color("terminal-selectionBackground", "editor-selectionBackground"),
    };
    for (const name of ANSI) {
      found[name.toLowerCase()] = color(`terminal-ansi${name}`);
      found[`bright${name}`] = color(`terminal-ansiBright${name}`);
    }
    document.body.style.background = background ?? "";
    return found;
  }

  function input(id, data, binary = false) {
    post({ type: "input", id, data, binary });
  }

  /** The keys a terminal in a web page gets wrong. True: xterm.js handles the key. */
  function keys(id, term, event) {
    const plain = !event.ctrlKey && !event.altKey && !event.metaKey;
    if (event.key === "Enter" && event.shiftKey && plain) {
      event.preventDefault();
      if (event.type === "keydown") {
        input(id, "\x1b\r"); // a new line in Claude's prompt, as VS Code's terminal setup sends
      }
      return false;
    }
    if (event.ctrlKey && event.shiftKey && event.code === "KeyC") {
      event.preventDefault();
      if (event.type === "keydown" && term.hasSelection()) {
        navigator.clipboard.writeText(term.getSelection());
      }
      return false;
    }
    // Left to the page: it pastes, and xterm.js reads the paste. The Cmd keys of macOS too.
    return !((event.ctrlKey && event.shiftKey && event.code === "KeyV") || event.metaKey);
  }

  function open(id) {
    const box = make("div", "terminal", stage);
    box.hidden = true;
    const link = (_event, url) => post({ type: "link", url });
    const term = new window.Terminal({
      allowProposedApi: true, // the Unicode 11 widths
      fontFamily: font.family,
      fontSize: font.size,
      scrollback: 5000,
      minimumContrastRatio: 4.5,
      theme: theme(),
      linkHandler: { activate: link },
    });
    const fit = new window.FitAddon.FitAddon();
    term.loadAddon(fit);
    term.loadAddon(new window.WebLinksAddon.WebLinksAddon(link));
    term.loadAddon(new window.Unicode11Addon.Unicode11Addon());
    term.unicode.activeVersion = "11";
    term.attachCustomKeyEventHandler((event) => keys(id, term, event));
    term.onData((data) => input(id, data));
    term.onBinary((data) => input(id, data, true));
    term.open(box);
    const entry = { box, term, fit, size: "" };
    terminals.set(id, entry);
    return entry;
  }

  /** Fit the terminal in front to the view, and say its size when it changed. */
  function size() {
    const entry = terminals.get(active);
    if (!entry || !stage.clientWidth || !stage.clientHeight) {
      return; // the view is hidden
    }
    entry.fit.fit();
    const { rows, cols } = entry.term;
    const next = `${rows}x${cols}`;
    if (next !== entry.size) {
      entry.size = next;
      post({ type: "size", id: active, rows, cols });
    }
  }

  let due;
  const soon = () => {
    clearTimeout(due);
    due = setTimeout(size, 60);
  };
  new ResizeObserver(soon).observe(stage);

  // --- the bar ---------------------------------------------------------------

  function drawTabs() {
    strip.replaceChildren();
    for (const tab of tabs) {
      const node = make("div", `tab ${tab.state}`, strip);
      node.classList.toggle("active", tab.id === active);
      node.setAttribute("role", "tab");
      node.setAttribute("aria-selected", String(tab.id === active));
      node.tabIndex = 0;
      node.title = `${tab.title}\n${tab.cwd}\n${tab.id}`;
      make("span", `dot ${tab.state === "attached" ? tab.mark || "alive" : "stopped"}`, node);
      make("span", "title", node, tab.title);
      button("tool close", node, "×", "Close the tab. The session keeps running.", () =>
        post({ type: "detach", id: tab.id }),
      );
      const choose = () => post({ type: "show", id: tab.id });
      node.addEventListener("click", choose);
      node.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          choose();
        }
      });
    }
  }

  function drawAbout() {
    const tab = tabs.find((t) => t.id === active);
    about.hidden = !tab;
    if (!tab) {
      return;
    }
    model.textContent = tab.model;
    context.hidden = !tab.context;
    if (tab.context) {
      const { percent, level, window: full } = tab.context;
      meter.className = `meter ${level}`;
      fill.style.width = `${Math.min(100, percent)}%`;
      used.textContent = full ? `${Math.round(percent)}% of ${full}` : tab.context.used;
      context.title = `Context: ${tab.context.used}${full ? ` of ${full}` : ""}`;
    }
    cost.textContent = tab.cost;
    cost.title = "Cost of this conversation";
    end.hidden = tab.state === "ended";
  }

  function drawStage() {
    empty.hidden = tabs.length > 0;
    for (const [id, entry] of terminals) {
      if (!tabs.some((t) => t.id === id)) {
        entry.term.dispose();
        entry.box.remove();
        terminals.delete(id);
      }
    }
    for (const tab of tabs) {
      const entry = terminals.get(tab.id) ?? open(tab.id);
      entry.box.hidden = tab.id !== active;
    }
  }

  // --- messages --------------------------------------------------------------

  const handlers = {
    font(message) {
      font = message.font;
      for (const entry of terminals.values()) {
        entry.term.options.fontFamily = font.family;
        entry.term.options.fontSize = font.size;
      }
      soon();
    },
    tabs(message) {
      const switched = message.active !== active;
      tabs = message.tabs;
      active = message.active;
      drawTabs();
      drawAbout();
      drawStage();
      const entry = terminals.get(active);
      if (switched && entry) {
        entry.size = ""; // say it again: the last viewer to say a size sets the session's
      }
      size();
      if (switched) {
        entry?.term.focus();
      }
    },
    data(message) {
      terminals.get(message.id)?.term.write(new Uint8Array(message.data));
    },
    notice(message) {
      terminals.get(message.id)?.term.write(`\r\n\x1b[0;7m ${message.text} \x1b[0m\r\n`);
    },
    reset(message) {
      const entry = terminals.get(message.id);
      if (entry) {
        entry.term.reset();
        entry.size = "";
        soon();
      }
    },
    focus() {
      terminals.get(active)?.term.focus();
    },
  };
  window.addEventListener("message", ({ data }) => handlers[data.type]?.(data));

  // A change of the color theme changes the classes and the variables of the page.
  const recolor = () => {
    const next = theme();
    for (const entry of terminals.values()) {
      entry.term.options.theme = next;
    }
  };
  new MutationObserver(recolor).observe(document.body, { attributeFilter: ["class"] });
  recolor();
  drawAbout();
  post({ type: "ready" });
})();
