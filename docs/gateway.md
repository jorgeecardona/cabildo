# Gateway

Use this when your claude.ai subscription runs out and you want Claude Code to keep working
without restarting sessions. The gateway is a small server on your machine. Claude Code sends its
model requests to it, and it chooses where each one goes.

Claude Code reads `ANTHROPIC_BASE_URL` once, when it starts. You set it once. After that, a change
of upstream is a change in the gateway, so no session restarts.

## Set it up

1. Add the upstreams to `~/.cabildo/config.toml`:

```toml
[gateway]
port   = 8787
policy = "failover"        # or "round_robin"

[gateway.upstream.subscription]
kind     = "passthrough"   # sends the request on with the client's own login
base_url = "https://api.anthropic.com"

[gateway.upstream.bedrock]
kind     = "key"
base_url = "https://bedrock-mantle.us-east-1.api.aws/anthropic"
key      = "env:AWS_BEARER_TOKEN_BEDROCK"   # or op://Vault/Item/field, or file:/path
models   = { "claude-opus-5-5" = "anthropic.claude-opus-5-5" }
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

- `failover` tries the upstreams in the order of the file. `round_robin` rotates the first choice.
- An upstream that answers 429, 402, 401 or 403, or 400 with "credit balance", is set aside. The
  same request goes to the next upstream, and Claude Code never sees the error.
- A 429 is set aside for its `retry-after` time, 5 minutes if it has none. Billing errors wait an
  hour. A key that is refused waits 10 minutes.
- If every upstream is set aside, the client gets the last real answer.
- Any other answer, a 5xx included, goes to the client unchanged.

`cabildo gateway status` shows each upstream and for how long it is set aside.
`cabildo service reload` reads the config again and keeps streams that are open.

## What to know

- Upstream `kind = "passthrough"` forwards the client's `Authorization` and `anthropic-beta`.
  `kind = "key"` replaces them with its own key and sends it as `x-api-key` and as a bearer token
  (`auth = "x-api-key"` or `"bearer"` sends one).
- `models` renames the model for that upstream. Bedrock Mantle uses its own ids.
- Switching upstream starts a new prompt cache, so the first request after a switch costs more.
- The server listens on `127.0.0.1` only. Any program on your machine can use it, and so can spend
  your keys. It reads only `~/.cabildo/config.toml`, never a repo's config.
- A Bedrock API key needs access to the Mantle endpoint, which serves the Anthropic Messages API.
  The Bedrock Invoke API uses another format and is not supported.
- Not checked against a live Bedrock key yet: which header Mantle expects, and whether Claude Code
  stops by itself on a usage limit before it sends a request.
