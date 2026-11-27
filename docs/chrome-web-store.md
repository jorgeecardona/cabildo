# Publish the Chrome extension

The GitLab job `chrome-extension` builds `cabildo-chrome.zip` on `main` and, when its variables
are set, uploads each new version to the Chrome Web Store and submits it for review.

To release a version, raise `version` in `src/cabildo/browser/extension/manifest.json` and push.
Build the zip yourself with:

```bash
uv run --no-project scripts/chrome_store.py package
```

## First version, once

1. Register a developer account, verify its contact email and turn on 2-Step Verification.
2. In the [dashboard](https://chrome.google.com/webstore/devconsole), press **New item** and
   upload `cabildo-chrome.zip`. Do not submit it yet.
3. Copy the public key from the **Package** tab into `key` of the manifest, and the item ID into
   `EXTENSION_ID` of `browser/chrome_install.py`. Push. Without this, the store's copy cannot
   connect: the local side allows one extension ID.
4. Fill the tabs with the texts below, then submit for review.

## Publish from CI, once

1. In the [Google Cloud console](https://console.cloud.google.com), enable the
   **Chrome Web Store API** in a project, create a service account with no roles, and create a
   JSON key for it.
2. In the dashboard, under **Account**, add the service account's email.
3. In GitLab, under Settings, CI/CD, Variables, add with environment `chrome-web-store`,
   protected:

| Key | Type | Value |
|---|---|---|
| `CWS_SERVICE_ACCOUNT` | File | the JSON key |
| `CWS_PUBLISHER_ID` | Variable | publisher ID, from the dashboard's Account page |
| `CWS_ITEM_ID` | Variable | the item ID |

Google's guide:
[service accounts](https://developer.chrome.com/docs/webstore/service-accounts).

## Store listing tab

- Category: Developer Tools. Language: English.
- Icon: `src/cabildo/browser/extension/icons/128.png`.
- Screenshot: `store/chrome/screenshot.png`.
- Description:

```text
cabildo lets the coding agents on your computer use the tabs you choose, in your own Chrome,
with the sites you are already signed in to.

Agents reach only the web pages in a tab group named "cabildo". Press "Share this tab" in the
popup to add one, turn the switch off to stop every agent, or press Cancel on Chrome's
debugging bar.

It needs the free, open source cabildo program on the same computer (Linux or macOS):
  uv tool install cabildo
  cabildo browser extension install

The extension talks only to that program, over Chrome's native messaging.
Source code and documentation: https://gitlab.com/cardona-oss/cabildo
```

## Privacy tab

Single purpose:

```text
Let the cabildo agents on the user's computer read and operate the web pages in the tabs the user
shares, in the tab group named "cabildo".
```

Permission justifications:

| Permission | Text |
|---|---|
| `debugger` | Reads and operates the pages of shared tabs (click, type, screenshot) through the DevTools Protocol. Commands are limited to a fixed list and to tabs of the "cabildo" group. |
| `nativeMessaging` | Connects to the cabildo program on the user's computer, the only party the extension talks to. |
| `tabs` | Reads the address and title of shared tabs, to refuse pages that are not web pages and to list them in the popup. |
| `tabGroups` | Finds the "cabildo" tab group, which is how the user chooses the shared tabs. |
| `storage` | Stores whether the user turned the extension off. |
| `alarms` | Connects again after the local program restarts. |

Remote code: yes. The reason:

```text
Through the chrome.debugger API only, the local cabildo program of the user sends DevTools
commands, including JavaScript to evaluate in pages of shared tabs. The extension itself loads
and evaluates no remote code.
```

Data usage: select **Website content** and **Web history**, and the three certifications. Read the
[privacy policy](chrome-extension-privacy.md) first: the certification is yours.

Privacy policy URL: `https://cardona-oss.gitlab.io/cabildo/chrome-extension-privacy/`

## Test instructions tab

```text
The extension needs a local program. On Linux or macOS:
1. uv tool install cabildo
2. cabildo browser extension install
3. Reload the extension. Its popup says "Connected".
4. Press "Share this tab" on any web page. To drive it, add the MCP server
   "cabildo browser serve --chrome" to an MCP client.
```

Review can take longer for extensions with the `debugger` permission, and Google can refuse it.
