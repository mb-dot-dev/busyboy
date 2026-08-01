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
```

Text stays on the display until it is replaced, cleared, or its `--timeout`
expires. Anything too wide for the 72x16 front display scrolls; pass
`--scroll-rate 0` to switch that off.

Successful commands print nothing and exit 0. Failures print one line to
stderr and exit 1. Use `--verbose` to see the underlying requests.
