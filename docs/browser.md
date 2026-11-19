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

The file is `--storage-state` of the `cabildo-browser` entry, or
`~/.claude/mcp-servers/playwright/storage-state.json` when there is none. To save somewhere else,
pass `--storage-state PATH` to `cabildo browser login`.

A login that an agent browser makes can be kept too. After you log in in that window (two-step
verification included), the agent calls `browser_save_logins(["splitwise.com"])`. Only cookies of
the named domains are merged into the file, under a lock, so browsers that run at the same time
do not overwrite each other. The first page result of a server also says when the file is missing,
empty or older than 30 days.

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
`browser_file_upload`, `browser_resize`, `browser_login`, `browser_save_logins` and
`browser_close`. Parameter names are snake_case, for
example `double_click` and `full_page`.

Not available yet: `browser_drag`, `browser_drop`, `browser_find`, `browser_network_request` and
`browser_run_code_unsafe`.

## Log in from 1Password

Use this when a saved login has expired. The agent calls `browser_login`, and the server fills the
form from 1Password. The password never reaches the agent.

1. Install `op`, unlock 1Password, and turn on Settings > Developer > Integrate with 1Password
   CLI.
2. Give each Login item the website URL of its site. That URL is what ties the item to a page.

No config is needed. The agent calls `browser_login(username_target, password_target)` on the
login page. The server lists your Login items (`op item list`) and uses the one whose website URL
has the page's origin: same `https`, host and port. The targets are refs from `browser_snapshot`.
Set `submit` to press Enter in the last field.

If several items match, the answer lists them (title and vault). The agent calls again with
`site` set to one title. A title only picks among items already matched to the page.

Optional: pin a login in `~/.cabildo/config.toml`. A table for the page's origin wins over an
item, and lets you name the fields:

```toml
[browser.login.github]
origin   = "https://github.com"
username = "op://Cabildo/GitHub/username"
password = "op://Cabildo/GitHub/password"
otp      = "op://Cabildo/GitHub/one-time password?attribute=otp"
```

`otp` is optional. The file is read only from `~/.cabildo/config.toml`, never from a repo. With
`site = "github"` the agent picks a table by name.

Every target is optional. A site that asks for the one-time code on a second page needs a second
call with only the code: `browser_login(otp_target=...)`. A site that asks for the password
on its own page needs a call with only `password_target`. Each call is checked as in the rules
below. A login found without a table reads `username`, `password` and `one-time password` from the
item.

Rules the server enforces:

- The page must be on the login's origin exactly (the table's `origin`, or the item's website URL):
  same scheme, host and port. Otherwise nothing is read and nothing is filled.
- Each field must be in a document on that origin. A frame from another origin is refused.
- The password goes only into a password field.
- The password and the code are hidden in every tool result until the page navigates. While they
  are in the page, `browser_evaluate` is refused.
- The agent can name a site or an item title. It cannot pass an `op://` reference of its own.
