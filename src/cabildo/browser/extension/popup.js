// The popup: the state of the connection, the shared tabs, and a switch. Texts that come from web
// pages (titles, addresses) are set as text, never as HTML.
const byId = (id) => document.getElementById(id);

function draw(state) {
  const { connected, client, error, paused, tabs, share } = state;
  let status = "Not connected";
  let detail = error || "Waiting for cabildo.";
  if (paused) {
    status = "Off";
    detail = "Agents cannot use this browser.";
  } else if (client) {
    status = "In use";
    detail = client;
  } else if (connected) {
    status = "Connected";
    detail = "No agent is using this browser.";
  }
  byId("status").textContent = status;
  byId("detail").textContent = detail;
  byId("dot").className = `dot ${client ? "used" : connected ? "ready" : ""}`;
  byId("allow").checked = !paused;

  byId("tabs").replaceChildren(
    ...tabs.map((tab) => {
      const row = document.createElement("li");
      const text = row.appendChild(document.createElement("div"));
      text.appendChild(document.createElement("strong")).textContent = tab.title || tab.url;
      text.appendChild(document.createElement("small")).textContent = tab.usable
        ? tab.url
        : "Not a web page: agents cannot use it";
      const remove = row.appendChild(document.createElement("button"));
      remove.type = "button";
      remove.textContent = "×";
      remove.title = "Stop sharing this tab";
      remove.addEventListener("click", () => ask({ type: "unshare", tabId: tab.id }));
      return row;
    }),
  );
  byId("none").hidden = tabs.length > 0;
  byId("share").hidden = !share;
}

async function ask(message) {
  draw(await chrome.runtime.sendMessage(message));
}

byId("allow").addEventListener("change", (e) => ask({ type: "pause", value: !e.target.checked }));
byId("share").addEventListener("click", async () => {
  const [current] = await chrome.tabs.query({ active: true, currentWindow: true });
  await ask({ type: "share", tabId: current.id });
});
ask({ type: "state" });
setInterval(() => ask({ type: "state" }), 1500);
