# Gateway

Use this when your claude.ai subscription runs out and you want Claude Code to keep working
without restarting sessions. The gateway is a small server on your machine. Claude Code sends its
model requests to it, and it chooses where each one goes.

Claude Code reads `ANTHROPIC_BASE_URL` once, when it starts. You set it once. After that, a change
of upstream is a change in the gateway, so no session restarts.

## Set it up

1. Add the upstreams to `~/.cabildo/config.toml`. They are tried in the order of the file:

```toml
[gateway]
port = 8787

[gateway.upstream.subscription]
kind     = "passthrough"   # sends the request on with the client's own login
base_url = "https://api.anthropic.com"

[gateway.upstream.bedrock]
kind         = "key"
base_url     = "https://bedrock-mantle.us-east-1.api.aws/anthropic"
key          = "env:AWS_BEARER_TOKEN_BEDROCK"   # or op://Vault/Item/field, or file:/path
server_tools = false       # Bedrock has no WebSearch and the like
models       = { "claude-opus-5-5" = "anthropic.claude-opus-5-5", "claude-haiku-4-5" = "anthropic.claude-haiku-4-5" }
```

2. Install and start it as its own service:

```bash
cabildo service install --gateway
cabildo gateway status
```

3. Point Claude Code at it, in `~/.claude/settings.json`, then restart your sessions once:

```json
{ "env": { "ANTHROPIC_BASE_URL": "http://127.0.0.1:8787" } }
```

Remove `apiKeyHelper` from the settings. With a helper set, Claude Code sends its key instead of
your subscription login.

## How a request is routed

- The upstreams are tried in the order of the file: subscription first, then your keys, then
  the fallbacks.
- An upstream that answers 429, 402, 529, 401 (a key), 403 (a key), or 400 with "credit balance"
  is set aside. The same request goes to the next upstream, and Claude Code never sees the error.
- A 429 is set aside for its `retry-after` time, else until the unified reset time, else 60 s
  for a key and 5 minutes for the subscription. It is set aside for that model only, so Opus can
  run out while Haiku still works. Billing and a refused key are set aside for the whole upstream.
- A 401 or 403 from the subscription is one session's login. It goes to Claude Code, which
  refreshes it. The subscription is not set aside.
- If every upstream is set aside, the client gets the last real answer.
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
each request, so it needs no restart. `cabildo service reload` reads the config file again and
keeps streams that are open.

## What a request may reach

- `models` on a key upstream renames the model, and it is also a list of the models that upstream
  serves. A model that is not in it skips the upstream. Map the small background model too.
- `server_tools = false` skips the upstream for a request that uses a server tool such as
  WebSearch.
- A key upstream gets `anthropic-beta` values without `oauth-*`. Set `drop_betas` to change it.
- A key upstream only gets `/v1/messages` paths.
- A skipped upstream is not set aside. If every upstream is skipped, the answer is a 502 that
  says so.

## What to know

- `kind = "passthrough"` forwards the client's `Authorization` and `anthropic-beta`.
  `kind = "key"` replaces the client's credential with its own key, sent as `x-api-key`
  (`auth = "bearer"` or `"both"` change that).
- Switching upstream starts a new prompt cache, so the first request after a switch costs more.
- The server listens on `127.0.0.1` only and refuses any request that has an `Origin` header or
  another `Host`, so a web page cannot use it. Any program on your machine can still call it and
  spend your keys. To require a secret, set `token = "env:NAME"` and add
  `ANTHROPIC_CUSTOM_HEADERS="x-cabildo-gateway: <the secret>"` to Claude Code's `env`.
- It reads only `~/.cabildo/config.toml`, never a repo's config. It logs one line per request
  (upstream, model, status, time) and never headers or bodies.
- A Bedrock API key needs access to the Mantle endpoint, which serves the Anthropic Messages API.
  The Bedrock Invoke API uses another format and is not supported.
- Not checked live yet: which header Mantle expects with a real key, and whether Claude Code stops
  by itself on a usage limit before it sends a request.
