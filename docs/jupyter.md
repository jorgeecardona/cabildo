# Jupyter runners

A runner is a Jupyter Server that a session starts in a directory. Agents run cells on it with MCP
tools. You open the same kernel in your browser.

## Start one and run a cell

1. Start a runner in a directory:

   ```bash
   cabildo jupyter start analysis --cwd ~/stuff/projects/crm
   ```

2. Run a cell:

   ```bash
   cabildo jupyter run analysis "x = 6 * 7; print(x)"
   cabildo jupyter run analysis "print(x)" --notebook notes/plots.ipynb
   ```

3. Open it in your browser (JupyterLab, or one notebook):

   ```bash
   cabildo jupyter open analysis
   cabildo jupyter open analysis notebooks/plots.ipynb
   ```

`cabildo jupyter status` lists the runners. `cabildo jupyter stop analysis` ends one with its
kernel. `cabildo jupyter pause analysis` stops it using the CPU and keeps its memory (the kernel
and its variables stay); `cabildo jupyter resume analysis` continues it. A paused runner refuses
cells.

## In the workspace

The left panel has a `runners` group while a runner runs: `●` running, `‖` paused, then the CPU
and memory of the server with its kernels. Press `J` for the runners screen:

| Key | Does |
|---|---|
| `k` | stop the selected runner |
| `p` | pause it, or resume it when paused |
| `o` or Enter | open it in your browser |
| `q` or Esc | close the screen |

An agent does the same with the MCP tools `jupyter_start`, `jupyter_run`, `jupyter_open`,
`jupyter_list`, `jupyter_pause` and `jupyter_stop`. The session chooses `cwd`. Without it, the runner starts in
the project root.

## What starts

`cabildo` runs `uv run --no-project --with jupyterlab --with ipykernel python -m
jupyter_server` in a detached process. Cabildo itself gets no Jupyter dependency. The first start
resolves packages and can take a minute. `uv` must be on `PATH`.

The runner outlives the session that started it and a daemon reload. A reboot ends it.

## Configure

Runners are set in `~/.cabildo/config.toml`. A repo's config is never read, because a runner runs
code as you.

```toml
[jupyter]                      # defaults for every runner
python = "3.12"                # uv --python: a version or an interpreter path
packages = ["numpy", "pandas"] # added to the environment
project = "~/stuff/projects/crm" # a uv project: its locked environment is the kernel's

[jupyter.runners.analysis]     # one named runner
cwd = "~/stuff/projects/crm"   # a session's cwd wins over this
packages = ["polars"]          # replaces the default list for this runner
kernelspec = "python3"
startup = "import numpy as np" # run once each time the runner makes its kernel
port = 8899                    # 0 (default): a free port

[jupyter.runners.analysis.env] # set in the server, so every kernel has it
DATABASE_URL = "postgresql:///crm"
```

A name that is not in the config gets the `[jupyter]` defaults.

`project` makes the kernel run in a uv project's own locked environment (`uv run --project`), so
`import` finds the project's packages and its versions. `packages` are added on top. The folder
needs a `pyproject.toml`.

## Cells

- One cell runs at a time per runner. A second call waits.
- After `timeout` seconds (default 30) the cell is interrupted and the result has
  `status = "timeout"` with the output so far.
- Output over 8000 characters is cut in the middle.
- An image output is counted in `images` and not returned yet.
- Without `notebook`, a cell runs on the runner's own kernel. It keeps its variables between
  calls, and no notebook shows it.
- With `notebook="analysis/plots.ipynb"` (a path inside the runner's directory), a cell runs on
  that notebook's kernel session. The file and its folders are made when missing, and `startup`
  runs once when the session is made. When you open that notebook with `cabildo jupyter open`
  (or `jupyter_open`), JupyterLab joins the same session: you and the agent share the variables.
  The cells an agent runs still do not appear in the open notebook.

## The link and the token

Each runner has a token. It is stored in `~/.cabildo/jupyter/<name>/token` (mode 600). It is not in
a command line, a log or a message.

`cabildo jupyter open` and the `jupyter_open` tool give two things:

- `link`: a `file://` page that redirects to the runner with the token. Click it, or open it.
- `url`: the server address without the token, for reference.

The server listens on `127.0.0.1` only.

## Risk

`jupyter_run` runs Python as you. It skips the checks that cabildo applies to the `Bash` tool.
There is no sandbox. A runner that you do not need should be stopped.
