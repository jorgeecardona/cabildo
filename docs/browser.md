# The agent browser

Run `cabildo browser` to see and manage the browser that your agents use. Every agent session
starts its own visible Chrome, so these commands show what is running and keep it healthy.

## Commands

```bash
cabildo browser status
```

Lists the browser servers and the Chrome processes under them, with age and memory. Also shows
whether the saved login is empty or old.

```bash
cabildo browser reap --dry-run
cabildo browser reap
```

Stops the servers whose Claude session is gone, and Chrome processes that lost their server.
`--dry-run` only lists them. `--all` also stops servers of live sessions, except the one that runs
the command; add `--include-mine` to stop that one too.

```bash
cabildo browser login
```

Opens the profile that holds your logins in plain Chrome, with no automation flag, so Google
sign-in works. Log into your sites, then close the window. A second window then opens and closes
by itself: it reads the cookies. They go to the storage-state file with mode 600, and new agent
browsers start from it. Running agent browsers add the new cookies on their next tool call.

```bash
cabildo browser doctor
```

Reads the `cabildo-browser` entry in `~/.claude.json` and exits 1 if it breaks a rule:

- `--isolated` must be set, so no two agents share a profile lock.
- `--user-data-dir` must not be set.
- Nothing may turn headless on: not `--headless`, not an environment variable, not a config file.

## Use cabildo as the browser server

`cabildo browser serve` is an MCP server built on the Python Playwright package. To use it, set
the `cabildo-browser` entry in `~/.claude.json` to:

```json
{
  "command": "cabildo",
  "args": ["browser", "serve", "--isolated", "--storage-state", "/home/you/.claude/mcp-servers/playwright/storage-state.json"]
}
```

The server starts Chrome on the first tool call. It has no headless mode: `--headless` and
`--user-data-dir` are refused.

It offers these tools: `browser_navigate`, `browser_navigate_back`, `browser_snapshot`,
`browser_click`, `browser_type`, `browser_fill_form`, `browser_press_key`, `browser_hover`,
`browser_select_option`, `browser_take_screenshot`, `browser_evaluate`, `browser_wait_for`,
`browser_tabs`, `browser_console_messages`, `browser_network_requests`, `browser_handle_dialog`,
`browser_file_upload`, `browser_resize`, `browser_login` and `browser_close`. Parameter names are snake_case, for
example `double_click` and `full_page`.

Not available yet: `browser_drag`, `browser_drop`, `browser_find`, `browser_network_request` and
`browser_run_code_unsafe`.

## Log in from 1Password

Use this when a saved login has expired. The agent calls `browser_login`, and the server fills the
form from 1Password. The password never reaches the agent.

1. Put the logins in a vault the 1Password CLI can read, and install `op`.
2. Add one table per site to `~/.cabildo/config.toml`:

```toml
[browser.login.github]
origin   = "https://github.com"
username = "op://Cabildo/GitHub/username"
password = "op://Cabildo/GitHub/password"
otp      = "op://Cabildo/GitHub/one-time password?attribute=otp"
```

`otp` is optional. The file is read only from `~/.cabildo/config.toml`, never from a repo.

The agent calls `browser_login(site, username_target, password_target)`. The targets are refs
from `browser_snapshot`. Set `submit` to press Enter in the last field.

Rules the server enforces:

- The page must be on the site's `origin` exactly: same scheme, host and port. Otherwise nothing
  is read and nothing is filled.
- Each field must be in a document on that origin. A frame from another origin is refused.
- The password goes only into a password field.
- The password and the code are hidden in every tool result until the page navigates. While they
  are in the page, `browser_evaluate` is refused.
- The agent names a site. It cannot pass an `op://` reference of its own.
