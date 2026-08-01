# busyboy

Console application to display various information on BUSYBar.

## Installation

```bash
uv tool install busyboy
```

## Configuration

Both variables are required:

| Variable | Meaning |
| --- | --- |
| `BUSYBOY_HOST` | Hostname or IP of the bar, e.g. `10.0.4.20` |
| `BUSYBOY_TOKEN` | API token for the bar |

`--host` and `--token` override them for a single invocation.

## Usage

```bash
export BUSYBOY_HOST=10.0.4.20
export BUSYBOY_TOKEN=your-token

# Show a message on the front display
busyboy text "BUILD OK"

# In colour, disappearing after 30 seconds
busyboy text "deploy failed" --color red --timeout 30

# Take it down again
busyboy clear

# Watch a GitHub Actions workflow on the front display until Ctrl+C
busyboy gh workflow ci.yml
```

Text stays on the display until it is replaced, cleared, or its `--timeout`
expires. Anything too wide for the 72x16 front display scrolls; pass
`--scroll-rate 0` to switch that off.

Successful commands print nothing and exit 0. Failures print one line to
stderr and exit 1. Use `--verbose` to see the underlying requests.

### Watching a GitHub Actions workflow

```bash
busyboy gh workflow ci.yml
busyboy gh workflow "Deploy" --repo octocat/hello-world --branch main --interval 30
```

`WORKFLOW` is a workflow id, filename, or display name. By default the
command watches the current git checkout's branch in `origin`'s repository,
polling every 10 seconds; override either with `--repo owner/name`,
`--branch`, or `--interval` (seconds). It keeps the repository on the top
row, the open pull request number (or the branch name, if there isn't one)
on the bottom row, and a status icon on the left, redrawing whenever the
workflow's status changes — and it runs until interrupted with Ctrl+C, at
which point it clears the display before exiting.

This needs a GitHub token, sourced separately from `BUSYBOY_TOKEN`: it reads
`gh auth token` if the `gh` CLI is installed and logged in, falling back to
the `GITHUB_TOKEN` environment variable.
