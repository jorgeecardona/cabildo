# Gateway

Use this when your claude.ai subscription runs out and you want Claude Code to keep working
without restarting sessions. The gateway is a small server on your machine. Claude Code sends its
model requests to it, and it chooses where each one goes.

Claude Code reads `ANTHROPIC_BASE_URL` once, when it starts. You set it once. After that, a change
of upstream is a change in the gateway, so no session restarts.

## Set it up

1. Put your Bedrock key in a file only you can read:

   ```bash
   mkdir -p ~/.config/cabildo
   install -m 600 /dev/null ~/.config/cabildo/bedrock.key
   printf '%s' "$AWS_BEARER_TOKEN_BEDROCK" > ~/.config/cabildo/bedrock.key
   ```

2. Add the upstreams to `~/.cabildo/config.toml`. They are tried in the order of the file:

   ```toml
   [gateway]
   port = 8788                # 8787 is the port of `cabildo viewer`

   [gateway.upstream.subscription]
   kind     = "passthrough"   # sends the request on with the client's own login
   base_url = "https://api.anthropic.com"

   [gateway.upstream.bedrock]
   kind         = "key"
   base_url     = "https://bedrock-mantle.us-east-1.api.aws/anthropic"
   key          = "file:~/.config/cabildo/bedrock.key"   # or op://Vault/Item/field
   server_tools = false       # Bedrock has no WebSearch and the like

   [gateway.upstream.bedrock.models]   # the models this upstream serves, and their names there
   "claude-opus-5-5"  = "anthropic.claude-opus-5-5"
   "claude-haiku-4-5" = "anthropic.claude-haiku-4-5"
   ```

3. Install and start it as its own service. This writes only the gateway unit and leaves the
   daemon unit as it is:

   ```bash
   cabildo service install --gateway
   cabildo gateway status
   ```

4. Point Claude Code at it, in `~/.claude/settings.json`, then restart your sessions once:

   ```json
   { "env": { "ANTHROPIC_BASE_URL": "http://127.0.0.1:8788" } }
   ```

   Remove `apiKeyHelper` from the settings. With a helper set, Claude Code sends its key
   instead of your subscription login.

The service does not see the variables of your shell. A key with `env:NAME` is empty there, and
the upstream is set aside with "no usable key". Use `file:` or `op://`, or set the variable in
`~/.config/systemd/user/cabildo-gateway.service.d/override.conf`.

## Check that it works

1. Send one prompt in a session.
2. Run `cabildo gateway status`. The `served` count of the first upstream goes up.
3. Read the log, one line per request, with `cabildo service logs -f`.

## How a request is routed

- The upstreams are tried in the order of the file. In the example above: the subscription, then
  bedrock.
- An upstream that answers 429, 402, 529, 401 (a key), 403 (a key), or 400 with "credit balance"
  is set aside. The same request goes to the next upstream, and Claude Code never sees the error.
- A set-aside upstream is tried last, not never. If every upstream refuses, the client gets the
  last refusal.
- A 429 is set aside for its `retry-after` time, else until the unified reset time, else 60 s
  for a key and 5 minutes for the subscription. A 403 from a key is set aside for 10 minutes.
  Both are set aside for that model only, so Opus can run out while Haiku still works.
- These are set aside for the whole upstream: billing (1 hour), a 401 from a key (10 minutes),
  a 529 (30 s), a key that cannot be read (10 minutes), and no connection (30 s).
- A 401 or 403 from the subscription is one session's login. It goes to Claude Code, which
  refreshes it. The subscription is not set aside.
- A timeout after the request was sent is a 504. It is never sent to a second upstream, because
  that could bill twice.
- Any other answer, a 5xx included, goes to the client unchanged.

## Choose the upstream yourself

```bash
cabildo gateway use bedrock --for 2h   # try bedrock first for 2 hours
cabildo gateway use auto               # back to the order of the file
cabildo gateway use                    # show the choice
cabildo gateway status                 # each upstream: ready or set aside, served, last error
```

The upstream you choose goes first. The others stay as fallbacks. The gateway reads the choice on
each request, so it needs no restart.

After you change the config file, run `cabildo service reload`. The gateway reads the file again
and keeps the streams that are open. The command also reloads the daemon. New gateway code needs
`cabildo service restart`, which cuts the streams that are open.

## What a request may reach

- `models` renames the model for that upstream, and it is also a list of the models that upstream
  serves. A model that is not in it skips the upstream. Map the small background model too.
- `server_tools = false` skips the upstream for a request that uses a server tool such as
  WebSearch.
- `drop_betas` is a list of glob patterns for `anthropic-beta` values. It replaces the default
  `["oauth-*"]` of a key upstream. A subscription upstream drops nothing by default.
- A key upstream only gets `/v1/messages` paths.
- A skipped upstream is not set aside. If every upstream is skipped, the answer is a 502 that
  says so.

## What to know

- `kind = "passthrough"` forwards the client's `Authorization` and `anthropic-beta`.
  `kind = "key"` replaces the client's credential with its own key, sent as `x-api-key`
  (`auth = "bearer"` or `"both"` change that).
- Switching upstream starts a new prompt cache, so the first request after a switch costs more.
- The server listens on `127.0.0.1` only. It refuses any request that has an `Origin` header or
  another `Host`, so a web page cannot use it. Any program on your machine can still call it and
  spend your keys. To require a secret, set `token = "file:/path"` in `[gateway]` and add
  `ANTHROPIC_CUSTOM_HEADERS="x-cabildo-gateway: <the secret>"` to Claude Code's `env`.
- It reads only `~/.cabildo/config.toml`, never a repo's config. It logs one line per request
  (upstream, model, status, time) and never headers or bodies.
- A Bedrock API key needs access to the Mantle endpoint, which serves the Anthropic Messages API.
  The Bedrock Invoke API uses another format and is not supported.
- Not checked live yet: which header Mantle expects with a real key, and whether Claude Code stops
  by itself on a usage limit before it sends a request.

## Undo

1. Remove `ANTHROPIC_BASE_URL` from `~/.claude/settings.json`.
2. Restart your sessions.
3. Stop the gateway:

   ```bash
   systemctl --user disable --now cabildo-gateway.service
   ```

`cabildo service uninstall` removes the daemon unit as well. Use the `systemctl` command to
remove only the gateway.
