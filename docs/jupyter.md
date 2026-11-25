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

`cabildo jupyter interrupt analysis` stops the running cell, like Ctrl-C, and keeps the variables.
`cabildo jupyter restart analysis` restarts the kernel: variables are lost and `startup` runs
again. Both take `--notebook PATH` for a notebook's kernel. The tools are `jupyter_interrupt` and
`jupyter_restart`.

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
| `r` | restart its kernel |
| `i` | interrupt the running cell |
| `o` or Enter | open it in your browser |
| `q` or Esc | close the screen |

An agent does the same with the MCP tools `jupyter_start`, `jupyter_run`, `jupyter_open`,
`jupyter_list`, `jupyter_pause` and `jupyter_stop`. The tools that edit cells are below. The session chooses `cwd`. Without it, the runner starts in
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
autostart = true               # the daemon starts it and starts it again when it ends
memory_max = "6G"              # the daemon pauses it above this much memory (server and kernels)

[jupyter.runners.analysis.env] # set in the server, so every kernel has it
DATABASE_URL = "postgresql:///crm"
```

A name that is not in the config gets the `[jupyter]` defaults.

`project` makes the kernel run in a uv project's own locked environment (`uv run --project`), so
`import` finds the project's packages and its versions. `packages` are added on top. The folder
needs a `pyproject.toml`.

## Edit a notebook that you have open

An agent edits the cells of a notebook while you watch it in JupyterLab:

| Tool | Does |
|---|---|
| `jupyter_cells` | list the cells: position, id, type, source, output |
| `jupyter_edit_cell` | replace a cell's source (or its type) |
| `jupyter_insert_cell` | add a cell at a position; makes the notebook when missing |
| `jupyter_delete_cell` | remove a cell |
| `jupyter_run_cell` | run a cell and store its outputs in it |
| `jupyter_run` with `add_cell` | run code and add it, with its outputs, as a new cell |

A cell is named by its id or its position (`"0"`, `"-1"`). `cabildo jupyter cells NAME --notebook
PATH` lists them from a shell.

Each edit reads the `.ipynb` through the server, changes it and writes it back. The runner
installs `jupyter-collaboration`, which sees the new file within about a second and updates your
open notebook, with no reload. Tested in a real browser: an inserted cell, an edited cell, and the
outputs and `[1]` of a run all appeared.

- **Your typing is protected.** JupyterLab saves your edits about a second after you stop typing.
  An edit waits until the file has not changed for 2 seconds, and gives up after 10 seconds with
  "someone is editing it". It also refuses when the file changed between its read and its write.
- **The guard.** While a runner serves a notebook, `Edit`, `Write` and `NotebookEdit` on that
  `.ipynb` are denied, and the message names the cell tools. A direct write would replace what you
  typed in the last second. Turn the guard off with `[jupyter] guard = false`.
- **Turn collaboration off** with `collaboration = false` on a runner. Then edits reach the file
  but an open notebook shows them only after you reload it.
- A cell that the agent runs with `jupyter_run_cell` runs the source that is in the file, not
  what you have typed and not yet saved.

## The daemon keeps your runners

The daemon (`cabildo service install`) checks the runners in `[jupyter.runners.<name>]` every 15
seconds:

- `autostart = true`: it starts the runner when it is not running. This brings it back after a
  crash, and after a reboot, because the daemon starts at login. Kernel variables are lost.
- A runner that you stopped (`cabildo jupyter stop`, `jupyter_stop`, `k` on the runners screen)
  stays stopped until you start it again.
- A runner that ends 5 times in an hour is left alone, with a line in the daemon log.
- `memory_max`: above this limit the runner is paused, with a line in the daemon log. Restart the
  kernel (`cabildo jupyter restart NAME`) to free the memory, then `resume`.
- A runner that a session starts on the fly, with no table in the config, is not supervised.

The runners are separate processes. A daemon reload or restart does not end them. Turn this off
with `python -m cabildo.daemon --no-jupyter`.

## Cells

- One cell runs at a time per runner. A second call waits.
- After `timeout` seconds (default 30) the cell is interrupted and the result has
  `status = "timeout"` with the output so far.
- Output over 8000 characters is cut in the middle.
- A plot is not returned as pixels. Each image is saved in
  `~/.cabildo/jupyter/<runner>/out/<time>-<id>/01.png`, and `jupyter_run` lists it in `images` with
  its `path`, `mime`, `width` and `height`. An agent calls `Read` on the PNG to see it. A plotly
  figure is saved as HTML and listed in `html`. A dataframe comes back as its text.
- When the text is cut, `full_text` is a file with all of it. `execution_count` is the kernel's
  `In [n]`. A runner keeps the files of its last 200 executions.
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
