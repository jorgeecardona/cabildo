// The service worker of the cabildo extension. It connects to the local program `cabildo browser
// extension host` and runs its commands on the web pages in the tabs of the "cabildo" tab group,
// and on nothing else. This file is where that limit, the list of allowed DevTools commands and the
// removal of cookie headers are enforced.

const HOST = "dev.cabildo.chrome";
const GROUP = "cabildo";
const RETRY = "connect";

// DevTools domains a command may use in a shared tab. Network and Target are limited to the
// methods below: the others reach cookies of any site, or other tabs.
const DOMAINS = new Set(["Accessibility", "DOM", "Emulation", "Input", "Log", "Page", "Runtime"]);
const METHODS = new Set([
  "Network.disable",
  "Network.enable",
  "Network.getRequestPostData",
  "Network.getResponseBody",
  "Network.setBypassServiceWorker",
  "Network.setCacheDisabled",
  "Network.setExtraHTTPHeaders",
  "Network.setUserAgentOverride",
  "Target.getTargetInfo",
  "Target.setAutoAttach",
]);
// In an allowed domain, but they read or delete cookies of any site.
const REFUSED = new Set(["Page.deleteCookie", "Page.getCookies"]);
// The only pages an agent may use. This extension's own pages have every permission it has, and
// file: pages show the files of the computer.
const WEB = /^(https?:|data:|blob:https?:|about:blank([?#]|$))/;

let port = null;
let client = null; // the name of the browser server that is connected
let error = "";
let paused = false;
const parts = [];
let shared = new Set(); // tab ids of the group, as last reported
const attached = new Set(); // tab ids with the debugger attached

const ready = (async () => {
  paused = Boolean((await chrome.storage.local.get("paused")).paused);
  // Chrome keeps the debugger attached when it stops this worker. Detaching fails, harmlessly,
  // for a tab another debugger holds.
  for (const target of await chrome.debugger.getTargets()) {
    if (target.attached && target.tabId) {
      await chrome.debugger.detach({ tabId: target.tabId }).catch(() => {});
    }
  }
})();

// --- the connection ----------------------------------------------------------

async function connect() {
  await ready;
  if (port || paused) {
    return;
  }
  const next = chrome.runtime.connectNative(HOST);
  port = next;
  error = "";
  next.onMessage.addListener(receive);
  next.onDisconnect.addListener(() => {
    error = chrome.runtime.lastError?.message ?? "";
    if (port === next) {
      port = null;
      setClient(null);
    }
  });
  badge();
}

function disconnect() {
  const old = port;
  port = null;
  old?.disconnect();
  setClient(null);
}

function post(message) {
  port?.postMessage(message);
}

/** `value` without cookie headers and cookie lists, for what Chrome reports about requests. */
function withoutCookies(value) {
  if (Array.isArray(value)) {
    return value.filter((v) => !/^(set-)?cookie$/i.test(v?.name ?? "")).map(withoutCookies);
  }
  if (!value || typeof value !== "object") {
    return value;
  }
  const kept = {};
  for (const [key, inner] of Object.entries(value)) {
    if (/^((set-)?cookie|(request)?headersText)$/i.test(key)) {
      continue;
    }
    kept[key] = /Cookies$/.test(key) && Array.isArray(inner) ? [] : withoutCookies(inner);
  }
  return kept;
}

function event(method, params) {
  if (client) {
    post({ type: "event", method, params });
  }
}

function badge() {
  chrome.action.setBadgeText({ text: client ? "on" : "" });
  chrome.action.setBadgeBackgroundColor({ color: "#1a7f37" });
}

function setClient(name) {
  client = name;
  badge();
  if (!name) {
    detachAll();
  }
}

async function receive(message) {
  if (message.type === "part") {
    parts[message.index] = message.data;
    if (message.index === message.count - 1) {
      receive(JSON.parse(parts.splice(0).join("")));
    }
    return;
  }
  if (message.type === "client") {
    setClient(message.name);
    if (message.name) {
      await sync();
    }
    return;
  }
  const { id, method, params } = message;
  try {
    if (!client) {
      throw new Error("no agent is connected");
    }
    const run = commands[method];
    if (!run) {
      throw new Error(`unknown command ${method}`);
    }
    post({ type: "result", id, result: await run(params ?? {}) });
  } catch (failed) {
    post({ type: "error", id, error: String(failed?.message ?? failed) });
  }
}

// --- the shared tabs ---------------------------------------------------------

async function groups() {
  const found = await chrome.tabGroups.query({});
  return found.filter((g) => g.title === GROUP);
}

async function groupTabs() {
  const found = [];
  for (const group of await groups()) {
    found.push(...(await chrome.tabs.query({ groupId: group.id })));
  }
  return found;
}

function webAddress(url) {
  try {
    return WEB.test(new URL(url).href); // the parsed address: lower case scheme, no spaces
  } catch {
    return false;
  }
}

/** Whether the tab shows a web page, and is not on its way to something else. */
function usable(tab) {
  const urls = [tab.url, tab.pendingUrl].filter(Boolean);
  return urls.length > 0 && urls.every(webAddress);
}

/** The tabs of the group an agent may use. */
async function webTabs() {
  return (await groupTabs()).filter(usable);
}

async function allowed(tabId) {
  const tab = (await groupTabs()).find((t) => t.id === tabId);
  if (!tab) {
    throw new Error(`tab ${tabId} is not in the "${GROUP}" tab group`);
  }
  if (!usable(tab)) {
    throw new Error(`tab ${tabId} does not show a web page`);
  }
}

/** A window that holds shared tabs only, so a new tab covers none of yours; else null. */
async function agentWindow() {
  const found = await groups();
  const ids = new Set(found.map((g) => g.id));
  for (const { windowId } of found) {
    if ((await chrome.tabs.query({ windowId })).every((t) => ids.has(t.groupId))) {
      return windowId;
    }
  }
  return null;
}

/** Report the tabs an agent gained or lost, and let go of the lost ones. */
let syncing = Promise.resolve();
function sync() {
  syncing = syncing.then(async () => {
    const now = new Set((await webTabs()).map((t) => t.id));
    for (const tabId of shared) {
      if (!now.has(tabId)) {
        await detach(tabId);
        event("tab.removed", { tabId });
      }
    }
    for (const tabId of now) {
      if (!shared.has(tabId)) {
        event("tab.added", { tabId });
      }
    }
    shared = now;
  }).catch(() => {}); // a tab closed while we looked: the next event syncs again
  return syncing;
}

async function share(tabId, windowId) {
  const [group] = (await groups()).filter((g) => g.windowId === windowId);
  if (group) {
    await chrome.tabs.group({ tabIds: tabId, groupId: group.id });
  } else {
    const groupId = await chrome.tabs.group({ tabIds: tabId, createProperties: { windowId } });
    await chrome.tabGroups.update(groupId, { title: GROUP, color: "green" });
  }
  await sync();
}

async function detach(tabId) {
  if (attached.delete(tabId)) {
    await chrome.debugger.detach({ tabId }).catch(() => {});
  }
}

function detachAll() {
  for (const tabId of [...attached]) {
    detach(tabId);
  }
}

// --- the commands ------------------------------------------------------------

const commands = {
  async info() {
    const version = /Chrome\/([\d.]+)/.exec(navigator.userAgent)?.[1] ?? "0";
    return { version, userAgent: navigator.userAgent };
  },

  async "tabs.list"() {
    return (await webTabs()).map((t) => t.id);
  },

  /** A new tab of the group, in a window of shared tabs only, or in a new one behind yours. */
  async "tabs.create"({ url }) {
    const target = url || "about:blank";
    if (!webAddress(target)) {
      throw new Error(`only web pages can be opened, not ${target}`);
    }
    const windowId = await agentWindow();
    let tab;
    if (windowId !== null) {
      tab = await chrome.tabs.create({ url: target, windowId });
    } else {
      const window = await chrome.windows.create({ url: target, focused: false });
      tab = window.tabs[0];
    }
    await share(tab.id, tab.windowId);
    return tab.id;
  },

  async "tabs.close"({ tabId }) {
    await allowed(tabId);
    await chrome.tabs.remove(tabId);
  },

  async "tabs.activate"({ tabId }) {
    await allowed(tabId);
    const tab = await chrome.tabs.update(tabId, { active: true });
    await chrome.windows.update(tab.windowId, { focused: true });
  },

  async "cdp.attach"({ tabId }) {
    await allowed(tabId);
    if (!attached.has(tabId)) {
      await chrome.debugger.attach({ tabId }, "1.3");
      attached.add(tabId);
      if (!client) {
        await detach(tabId); // the agent left, or you turned the switch off, while we attached
        throw new Error("no agent is connected");
      }
    }
    return chrome.debugger.sendCommand({ tabId }, "Target.getTargetInfo");
  },

  async "cdp.send"({ tabId, sessionId, method, params }) {
    const domain = method.split(".")[0];
    if (REFUSED.has(method) || !(DOMAINS.has(domain) || METHODS.has(method))) {
      throw new Error(`${method} is not allowed in your Chrome`);
    }
    await allowed(tabId);
    const target = sessionId ? { tabId, sessionId } : { tabId };
    if (method === "Page.navigate" && !webAddress(params.url)) {
      throw new Error(`only web pages can be opened, not ${params.url}`);
    }
    if (method === "Page.navigateToHistoryEntry") {
      const history = await chrome.debugger.sendCommand(target, "Page.getNavigationHistory");
      const entry = history.entries.find((e) => e.id === params.entryId);
      if (!entry || !webAddress(entry.url)) {
        throw new Error("that history entry is not a web page");
      }
    }
    return chrome.debugger.sendCommand(target, method, params);
  },
};

// --- Chrome's events ---------------------------------------------------------

/** Let go of a tab and tell the agent. A later sync reports it again if it may be used. */
async function drop(tabId) {
  shared.delete(tabId);
  await detach(tabId);
  event("tab.removed", { tabId });
}

chrome.debugger.onEvent.addListener((source, method, params) => {
  if (!attached.has(source.tabId)) {
    return;
  }
  // A page can also leave the web by going back in its history, which no command shows.
  if (method === "Page.frameNavigated" && !source.sessionId && !params.frame.parentId) {
    if (!webAddress(params.frame.unreachableUrl ?? params.frame.url)) {
      drop(source.tabId);
      return;
    }
  }
  const safe = method.startsWith("Network.") ? withoutCookies(params) : params;
  event("cdp.event", { tabId: source.tabId, sessionId: source.sessionId, method, params: safe });
});
// The person pressed "Cancel" on Chrome's debugging bar, or Chrome detached the tab.
chrome.debugger.onDetach.addListener((source, reason) => {
  if (!attached.delete(source.tabId)) {
    return;
  }
  shared.delete(source.tabId);
  event("tab.removed", { tabId: source.tabId });
  if (reason === "canceled_by_user") {
    requests.pause({ value: true }); // "Cancel" means stop: turn the switch off
  } else {
    sync();
  }
});
chrome.tabs.onUpdated.addListener((_id, change) => {
  if (change.groupId !== undefined || change.url !== undefined) {
    sync();
  }
});
for (const changed of [
  chrome.tabs.onCreated, // a tab opened from a shared one starts in the group
  chrome.tabs.onRemoved,
  chrome.tabs.onAttached,
  chrome.tabGroups.onUpdated,
  chrome.tabGroups.onRemoved,
]) {
  changed.addListener(() => sync());
}

// --- the popup ---------------------------------------------------------------

const requests = {
  async state() {},
  async share({ tabId }) {
    const tab = await chrome.tabs.get(tabId);
    await share(tab.id, tab.windowId);
  },
  async unshare({ tabId }) {
    await chrome.tabs.ungroup(tabId);
    await sync();
  },
  async pause({ value }) {
    paused = value;
    await chrome.storage.local.set({ paused });
    if (paused) {
      disconnect();
    } else {
      await connect();
    }
  },
};

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id !== chrome.runtime.id || !requests[message.type]) {
    return false;
  }
  (async () => {
    await ready;
    await requests[message.type](message);
    await connect();
    // A refused connection reports its error a moment later.
    await new Promise((done) => setTimeout(done, 150));
    const tabs = (await groupTabs()).map((tab) => ({
      id: tab.id,
      title: tab.title,
      url: tab.url,
      usable: usable(tab),
    }));
    const [current] = await chrome.tabs.query({ active: true, currentWindow: true });
    const share = Boolean(current && usable(current) && !tabs.some((t) => t.id === current.id));
    reply({ connected: Boolean(port), client, error, paused, tabs, share });
  })();
  return true;
});

// --- start -------------------------------------------------------------------

// While connected, the open port keeps this worker running. The alarm retries after a
// disconnect, and wakes the worker when Chrome stopped it.
chrome.alarms.create(RETRY, { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => connect());
chrome.runtime.onStartup.addListener(() => connect());
chrome.runtime.onInstalled.addListener(() => connect());
connect();
