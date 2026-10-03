# Security Policy

## Supported versions

cabildo is pre-1.0 (alpha). Security fixes go to `main` and are released in the
next version on [PyPI](https://pypi.org/project/cabildo/). Please run the latest
released version before reporting; older versions are not patched separately.

## Reporting a vulnerability

Do not open a public issue for a security problem. Report it privately by email
to **jorgeecardona@gmail.com** with:

- a description of the issue and its impact,
- the version (`cabildo --version`) and OS,
- steps or a minimal proof of concept to reproduce it.

You can expect an acknowledgement within a few days. Once a fix is released,
credit is given in the changelog unless you prefer to stay anonymous.

## Scope

cabildo coordinates local agents through one shared SQLite database, plus hook
scripts that enforce advisory locks and gate pushes. Reports that matter most:

- a way for one session to bypass a `file:` lease or the push guard that
  another session holds,
- command or SQL injection through a message body, handle, topic, claim, or
  config value that reaches a shell or a query,
- a spawned agent escaping its worktree, its tool allowlist, or the fleet
  depth/budget limits,
- data exfiltration through the optional cloud uplink (`cabildo sync`) or LAN
  sync (`cabildo lan`) beyond the configured `[uplink].level`.

## Trust model

cabildo assumes every process that can open the shared database is already
trusted: it is a coordination bus for agents on one machine, not a security
boundary between mutually distrusting users. The advisory locks and hook guards
prevent well-behaved agents from colliding; they are not designed to stop a
hostile local process that writes to the database directly. The LAN transport
stays on the local subnet by design (broadcasts never route off-LAN), and the
cloud uplink is opt-in and off by default. Reports that assume an attacker
already has local write access to the database, or that require enabling a
transport the operator did not turn on, are out of scope.
