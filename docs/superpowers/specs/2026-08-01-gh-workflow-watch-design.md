# `busyboy gh workflow` — live GitHub Actions status on the bar

Date: 2026-08-01
Branch: `feature/gh-workflow-watch`

## Goal

Watch a GitHub Actions workflow and keep its latest run's status on the BUSY Bar's front display,
refreshing every 10 seconds until the user presses Ctrl+C.

The display shows three things at once: the repository, the pull request (or branch) the run belongs
to, and the run's status as an icon matching GitHub's own UI.

## Command surface

```
busyboy gh workflow <workflow>  [--branch REF] [--repo OWNER/NAME]
                                [--interval SECONDS]
                                [--host H] [--token T] [--verbose]
```

`<workflow>` accepts any of three forms, resolved in this order:

1. a numeric workflow id (`161335`)
2. a workflow filename (`main.yaml`), matched against the basename of the workflow's `path`
3. a workflow display name (`CI`), matched against the workflow's `name`

GitHub's `/actions/workflows/{id}/runs` endpoint accepts only an id or a filename, never a display
name, so busyboy resolves all three itself by listing `/actions/workflows` once at startup.

| Option | Default |
|---|---|
| `--branch` | the current checkout's branch |
| `--repo` | `origin`'s owner/name |
| `--interval` | `10` (seconds) |
| `--host`, `--token`, `--verbose` | as the existing subcommands |

The command runs until interrupted. On Ctrl+C it clears what busyboy drew and exits 0.

`gh` is a new Click group under `main`, holding `workflow` as its only subcommand.

## Data flow

One tick:

```
git rev-parse --abbrev-ref HEAD ─┐
git remote get-url origin ───────┴─→ Repo(owner, name), branch
                                        │
    GET /repos/{o}/{n}/actions/workflows ─→ match id | path basename | name
                                        │
    GET .../workflows/{id}/runs?branch=&per_page=1 ─→ latest run
    GET /repos/{o}/{n}/pulls?head={o}:{branch}&per_page=1 ─→ PR number | None
                                        │
                        status+conclusion ──→ IconName
                                        │
              (repo_label, ref_label, icon) ──→ diff vs last drawn
                                        │  changed?
                        POST {bar}/api/display/draw
```

Repo, branch, and workflow id are resolved once at startup and cached — none can change under a
running watch. Runs and pull requests are re-fetched every tick.

### Redraw only on change

The tick compares the rendered triple `(repo_label, ref_label, icon)` against what was last drawn
and skips the draw when nothing moved. Re-sending an identical payload every 10 seconds would
restart the repo-name scroll animation, so a name longer than the text column would never finish
crossing the display.

**Confirmed against a real bar.** When a watched workflow's status changed and the tick issued a
redraw, the scrolling repo row jumped back to its starting position rather than continuing. The diff
is therefore load-bearing, not merely a traffic optimisation.

## Authentication

The GitHub token is resolved once at startup:

1. `gh auth token` (subprocess) — reuses the user's existing `gh` login
2. `GITHUB_TOKEN` environment variable
3. otherwise, exit 1 with a message naming both options

The token is never logged, never printed, and never included in an error message or traceback —
the same rule the project already applies to `BUSYBOY_TOKEN` (see the pydantic `ValidationError`
gotcha in `CLAUDE.md`).

## Modules

The project's rule is one job per module, with hard boundaries. Three new modules, two amendments.

| Module | Owns | Knows nothing about |
|---|---|---|
| `git.py` *(new)* | `current_branch()`, `origin_repo() -> Repo`; the `git` subprocess wrapper | GitHub, the bar, Click |
| `github.py` *(new)* | token resolution, workflow/run/PR queries, the `WorkflowRun` model | the bar, Click, git |
| `watch.py` *(new)* | `tick()`, the driver loop, the status→icon mapping | Click, argv |
| `bar.py` | gains `IconName`, `ICON_ASSETS`, `upload_icons()`, `build_workflow_payload()` | GitHub |
| `cli.py` | the `gh` group and `workflow` command | payload shapes |
| `exceptions.py` | gains a `BusyboyError` base above `BarError`, plus `GitHubError` and `GitError` | — |

`watch.py` is the only module that knows about both GitHub and the bar; that is its job. It holds
the status→icon mapping because the mapping is exactly a translation between the two domains, and
neither `github.py` nor `bar.py` may know the other exists.

`watch.py` splits into a `tick(state) -> state` function and a thin driver that loops over it, so
tests can exercise one cycle without a loop, a clock, or a signal.

### Changes to existing code

Both are forced by the feature, not opportunistic refactoring.

**`bar._request` hardcodes `DISPLAY_DRAW_PATH`** in the URL it builds and in the `BarAPIError` /
`BarRequestError` it raises. Asset upload targets a second path, so `_request` takes `path` as a
parameter and callers supply it.

**The exception hierarchy is rooted at `BarError`.** Git and GitHub failures are not bar failures,
but `cli._handle_errors` wants to catch one thing. A `BusyboyError` base goes above `BarError`,
with `GitError` and `GitHubError` as new siblings of it.

## Display

The front panel is 72x16 RGB. The layout divides it into an icon section and a two-row text column:

```
+----------------------------------------------------------------------+
| (icon)  mb-dot-dev/busyboy      <- scrolls when wider than 54px       |
| 12x12   #12                     <- or the branch name when no PR      |
+----------------------------------------------------------------------+
  x=2..13         x=18, width=54
```

- **Icon**: `ImageElement` at `x=2, y=2`, 12x12, spanning both rows vertically.
- **Row 1**: the repository as `owner/name`, scrolling at `DEFAULT_SCROLL_RATE` when it overflows.
- **Row 2**: `#<number>` when the branch has an open pull request, otherwise the branch name.

Both rows are `TextElement`s with `width=54`. They keep stable `id`s (`repo`, `ref`, `icon`) so a
redraw replaces rather than stacks, and the whole payload is scoped to `application_name="busyboy"`
so it never disturbs other applications.

### Row geometry, measured

`CLAUDE.md` records the `condensed` glyph box as 9 rows tall — too tall to stack twice inside 16
pixels, which is why the rows use `tiny`.

**Confirmed against a real bar:** `tiny` stacks twice at `y=1` and `y=9`, both rows fully visible,
neither clipped at the top nor overlapping. These values now live in the hardware-facts section of
`CLAUDE.md`, since the OpenAPI spec documents none of it.

Do not use `align` to position the rows. It is already known to clip text off the top of the front
display; position with explicit `y`.

### Icons

Six 12x12 RGBA PNGs ship inside the package, redrawn from GitHub's Octicons. Colors come from
GitHub's dark-mode palette, which is brighter and reads better on an LED matrix than the light-mode
one:

| Icon | Octicon | Color |
|---|---|---|
| `success` | check-circle-fill | `#3fb950` |
| `failure` | x-circle-fill | `#f85149` |
| `pending` | dot-fill | `#d29922` |
| `in_progress` | spinner arc | `#d29922` |
| `cancelled` | stop | `#8b949e` |
| `skipped` | skip | `#8b949e` |

`in_progress` is a static arc, not an animation. `AnimationElement` exists in the API but would
require producing and uploading an animation asset for no functional gain.

### Status mapping

GitHub's run `status` decides, except when it is `completed`, where `conclusion` takes over:

| `status` | `conclusion` | Icon |
|---|---|---|
| `queued`, `waiting`, `pending`, `requested` | — | `pending` |
| `in_progress` | — | `in_progress` |
| `completed` | `success` | `success` |
| `completed` | `failure`, `timed_out`, `startup_failure` | `failure` |
| `completed` | `cancelled` | `cancelled` |
| `completed` | `skipped`, `neutral` | `skipped` |
| `completed` | `action_required` | `pending` |

Any unrecognised status or conclusion maps to `pending` rather than raising — GitHub adds values
over time, and a watch loop should not die because of one.

### Asset upload

All six PNGs upload at startup via `POST /api/assets/upload?application_name=busyboy&file=<name>`
with a raw binary body. The upload is unconditional: the bar's API has no endpoint that lists an
app's existing assets, and six ~200-byte requests at startup cost less than the machinery to avoid
them.

## Error handling

| Failure | Behavior |
|---|---|
| No `gh`, no `GITHUB_TOKEN` | exit 1, one line to stderr naming both options |
| Not a git repository, or no `origin` remote | exit 1 |
| `--repo` not in `owner/name` form | exit 2 (Click usage error) |
| Workflow argument matches no workflow | exit 1, listing the available workflow names |
| GitHub 401 | exit 1 — an auth failure never self-heals |
| GitHub 403 or 429 carrying rate-limit evidence | log at DEBUG, leave the bar untouched, retry next tick |
| GitHub 403 without rate-limit evidence | exit 1 — a genuine credential or scope failure |
| GitHub 5xx, timeout, connection drop | log at DEBUG, leave the bar untouched, retry next tick |
| Malformed response body (not JSON, wrong shape, schema drift) | treated as transient — retry next tick |
| Bar unreachable mid-watch | same — `bar.py`'s existing retry-then-raise is caught by the tick |
| Asset upload fails at startup | exit 1 — the display would be missing its icon |
| Ctrl+C | clear the display, exit 0 |

This keeps the existing CLI contract for everything that happens before the loop starts (one line
to stderr, exit 1; Click usage errors exit 2), and deliberately departs from it inside the loop. A
watch process is expected to outlive a laptop sleeping or a wifi hiccup; a two-second blip should
not end it.

**Revised during implementation.** This table originally read "GitHub 401 or 403 → exit 1". Review
found that GitHub returns 403 for rate limiting as well as for bad credentials, so a rate-limited
watch would have died rather than waited — and 429 was not covered at all. Rate-limit evidence is
now what decides: a `Retry-After` header, or `x-ratelimit-remaining: 0`. Without it, a 403 is still
fatal. Malformed response bodies were likewise uncovered and crashed with a bare `KeyError` or
`pydantic.ValidationError` instead of a `BusyboyError`; they are now transient, on the grounds that
a body that fails to parse is more often a proxy or outage artifact than a permanent condition.

Under `--verbose` the root logger goes to DEBUG and startup errors propagate as tracebacks, as they
already do. Swallowed in-loop errors are logged at DEBUG regardless, so `--verbose` is how you see
them.

## Testing

Following the project's existing approach — drive the real functions against registered endpoints
rather than mocking anything internal.

- `responses` registers both `api.github.com` and the bar. No internal function of `github.py`,
  `bar.py`, or `requests` is mocked.
- Subprocesses (`git`, `gh auth token`) are monkeypatched at the `subprocess.run` boundary only.
- `time.sleep` is monkeypatched, as the existing retry test already does.

Cases:

- **`git.py`**: branch parsing, SSH and HTTPS remote URL forms, `.git` suffix stripping, not-a-repo
  and no-origin failures.
- **`github.py`**: token from `gh`, token from env, neither; workflow resolution by id, by filename,
  and by display name; no-match; latest run; PR found and no PR open.
- **`watch.py` `tick()`**: every row of the status mapping; unknown status falls back to `pending`;
  the no-PR branch-name fallback; an unchanged triple suppresses the draw; a changed triple issues
  it; a GitHub 500 leaves the previous state intact and raises nothing.
- **`watch.py` driver**: KeyboardInterrupt clears the display and returns cleanly.
- **`bar.py`**: `build_workflow_payload` produces two text elements and one image element with the
  expected ids, x/width, and asset paths; `upload_icons` posts all six assets.
- **`cli.py`**: `--repo` and `--branch` override detection; a bad `--repo` exits 2; auth failure
  exits 1 with one stderr line.

Coverage must stay above the configured 75% floor; the project currently sits around 92%.

## Out of scope

- Watching more than one workflow at once.
- The back display.
- `led_notification_color` on the draw payload — the bar can blink its status LED with the run
  status, but nothing in the request asked for it.
- Job-level or step-level detail. Only the run's overall status is shown.
- Re-resolving the repo, branch, or workflow id mid-watch.
