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

3. Restart the daemon. It serves the gateway when `[gateway]` exists, so there is no second
   service:

   ```bash
   cabildo service restart
   cabildo gateway status
   ```

4. Point every Claude Code session at it, including sessions started outside cabildo:

   ```bash
   cabildo gateway enable
   ```

   This sets `env.ANTHROPIC_BASE_URL` in `~/.claude/settings.json` (or in `$CLAUDE_CONFIG_DIR`).
   It keeps your other settings and saves the old file as `settings.json.bak`. It refuses when
   no gateway answers, or when the settings already name another base URL. Sessions that are
   already running keep their old setting until you restart them.

   Remove `apiKeyHelper` from the settings. With a helper set, Claude Code sends its key
   instead of your subscription login. `enable` warns when it finds one.

The daemon does not see the variables of your shell. A key with `env:NAME` is empty there, and
the upstream is set aside with "no usable key". Use `file:` or `op://`, or set the variable in
`~/.config/systemd/user/cabildo-daemon.service.d/override.conf`.

If the daemon is not running, every session that uses the gateway fails to connect. Run
`cabildo gateway disable` to go back to Anthropic directly.

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

## Bedrock in a region without Mantle

Use this when Bedrock must stay in one region and `bedrock-mantle.<region>.api.aws` answers
`not_found` for your models. `kind = "bedrock-invoke"` calls the Invoke API of
`bedrock-runtime.<region>.amazonaws.com` with the same Bedrock API key:

```toml
[gateway.upstream.bedrock-eu]
kind   = "bedrock-invoke"
region = "eu-central-1"                          # or base_url = "https://..."
key    = "file:~/.config/cabildo/bedrock.key"

[gateway.upstream.bedrock-eu.models]             # required: Bedrock names models its own way
"claude-opus-5-5" = "eu.anthropic.claude-opus-5"
```

For each request the gateway does what the Invoke API needs, and Claude Code sees no difference:

- The path becomes `/model/<id>/invoke`, or `/invoke-with-response-stream` for a streamed request.
  The id comes from `models`.
- The body loses `model` and `stream` and gains `anthropic_version`. The `anthropic-beta` values
  move into the body as `anthropic_beta`.
- Fields Bedrock refuses are changed or dropped, and the log says which (#60).
  `thinking.type: "enabled"` becomes `"adaptive"` (the token budget is dropped).
  `context_management` and `service_tier` are dropped, and so is `top_p` when `temperature` is set.
  A `temperature` other than 1 is dropped while thinking is on.
  Without this, a refused `context_management` makes Claude Code compact in a loop.
- A streamed answer arrives as AWS binary frames. The gateway decodes them and sends server-sent
  events.
- A Bedrock error (`{"message": ...}`) is sent as an Anthropic error, so Claude Code reads it.
- The key goes in `Authorization: Bearer`. The upstream only takes `/v1/messages`, and it has
  `server_tools = false` by default.

A model that is not in `models` skips the upstream. Map the background model too.

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
- `drop_fields` is a list of top-level body fields to remove for that upstream, for a `key` or
  `bedrock-invoke` upstream. Use it when an upstream answers 400 for a field Claude Code sends.
- A key upstream only gets `/v1/messages` paths.
- A skipped upstream is not set aside. If every upstream is skipped, the answer is a 502 that
  says so.

## What to know

- `kind = "passthrough"` forwards the client's `Authorization` and `anthropic-beta`.
  `kind = "key"` replaces the client's credential with its own key, sent as `x-api-key`
  (`auth = "bearer"` or `"both"` change that).
- Switching upstream starts a new prompt cache, so the first request after a switch costs more.
- A thinking block carries a signature that only the upstream that issued it accepts. When a
  conversation moves to another upstream, the gateway removes the thinking blocks of earlier
  assistant turns from that request. The client keeps its own copy and sends the blocks again, so
  the gateway keeps removing them for the rest of that conversation. The model loses its earlier
  reasoning, and the first request after a switch is a cache miss anyway. Manual thinking
  (`type: "enabled"`) needs the last turn to start with a thinking block, so it is turned off for
  such a request. A conversation is found by a hash of its first system block and first user
  message, and the gateway forgets it when the daemon restarts. If an upstream then answers 400
  about a thinking signature, the gateway sends the request once more to the same upstream
  without the blocks (#61).
- The server listens on `127.0.0.1` only. It refuses any request that has an `Origin` header or
  another `Host`, so a web page cannot use it. Any program on your machine can still call it and
  spend your keys. To require a secret, set `token = "file:/path"` in `[gateway]` and add
  `ANTHROPIC_CUSTOM_HEADERS="x-cabildo-gateway: <the secret>"` to Claude Code's `env`.
- It reads only `~/.cabildo/config.toml`, never a repo's config. It logs one line per request
  (upstream, model, status, time) and never headers or bodies.
- A `key` upstream sends the Anthropic Messages API, so a Bedrock `key` upstream needs the Mantle
  endpoint. Not every region has one for your account. Use `bedrock-invoke` there (next section).
- Not checked live yet: which header Mantle expects with a real key, and whether Claude Code stops
  by itself on a usage limit before it sends a request.

## Reload and restart

`cabildo service reload` keeps the gateway up. The daemon stops accepting, waits up to 2 minutes
for the open requests, and hands the listening socket to the new process. Connections that arrive
in between wait in the socket and are served by the new process. A request still open after
2 minutes is cut. `[gateway]` changes in the config apply at a reload.

`cabildo service restart` cuts the open model streams of every session.

`cabildo gateway serve` runs the same server in the foreground, for debugging. Stop the daemon's
gateway first, or give it another `--port`.

## Undo

1. Remove the setting from Claude Code:

   ```bash
   cabildo gateway disable
   ```

2. Restart your sessions.

`disable` only removes a base URL that points to `127.0.0.1`. To stop serving, delete `[gateway]`
from the config and run `cabildo service reload`, or start the daemon with `--no-gateway`.

## Anthropic's terms

Read this before you run `cabildo gateway enable`. The terms do not say clearly that this setup
is allowed or forbidden. This page is not legal advice. Checked on 2026-11-21 against the pages
named here.

What the documents say:

- The [legal and compliance page](https://code.claude.com/docs/en/legal-and-compliance) says
  OAuth login "is intended exclusively for purchasers of Claude Free, Pro, Max, Ultra, Team, and
  Enterprise subscription plans and is designed to support ordinary use of Claude Code and other
  native Anthropic applications". It forbids third-party developers to "route requests through
  Free, Pro, Max, or Ultra plan credentials on behalf of their users", and says developers "may
  not collect, store, or intermediate Claude.ai credentials or session tokens".
- The [Usage Policy](https://www.anthropic.com/legal/aup) (effective 2026-11-12), under "Do Not
  Abuse Our Platform", forbids reselling, proxying or giving access to Claude "through
  unauthorized means, including routing requests through consumer subscriptions". It also
  forbids bypassing product guardrails without authorization.
- The [LLM gateway page](https://code.claude.com/docs/en/llm-gateway) documents `ANTHROPIC_BASE_URL`
  and says a gateway that passes subscription traffic on to Anthropic "must forward
  `Authorization` and the OAuth capability in `anthropic-beta`".

How this gateway relates to them:

- The `passthrough` upstream relays the request that Claude Code sends, for one person on one
  machine. It does not store the token, sign in, or serve anyone else. The text above is written
  about third-party developers who serve other users. Nothing in it names a personal relay.
- The words "intermediate" and "unauthorized means" are not defined. Anthropic can read them
  more widely than this page does.
- A `key` or `bedrock-invoke` upstream uses your own paid credentials. It does not use the
  subscription. The documents do not mention a switch when the subscription limit is reached.
  Whether that counts as getting around a limit is not stated.

To be sure, ask Anthropic through the "contact sales" link on the legal page, before you use it.

