# Workflow Name in the Bottom Row Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Append the watched workflow's display name to the bottom row of `busyboy gh workflow`, so a bar showing a red icon says *which* workflow failed.

**Architecture:** Three changes in three layers. `bar.py` gains a public `strip_undisplayable` for text that is one *component* of a row (drops non-ASCII rather than replacing it, may return `""`). `watch.Target` swaps its bare `workflow_id: int` for the whole `github.Workflow` that `resolve_workflow` already returns. `watch.render` then composes `ref_label` as the ref, a space, and the stripped workflow name. `Screen` gains no field — the name is a startup-time constant — so the redraw diff that keeps the scroll animation from restarting is untouched.

**Tech Stack:** Python 3.14, pydantic, Click, requests, pytest + `responses`, `uv`, `ruff`, `ty`.

**Spec:** `docs/superpowers/specs/2026-08-02-workflow-name-in-bottom-row-design.md`

## Global Constraints

- Branch: `workflow-name-in-bottom-row`, already created and checked out. Do not merge to `main`.
- Every command runs through `make` (which wraps `uv run --frozen ...`). A single test: `uv run --frozen pytest tests/test_watch.py::test_name -v`.
- `make test` (= `make lint` then `make unit`) must pass before each commit. `make lint` runs `ruff check`, `ruff format --check`, and `ty check`.
- Ruff: 120-char lines, double quotes, PEP 257 docstrings. `force-sort-within-sections` sorts `import x` and `from x import y` together by module name.
- Module boundaries are load-bearing (see CLAUDE.md): `bar.py` knows nothing about GitHub; `watch.py` is the only module importing both `bar` and `github`; `watch.py` knows nothing about Click. This plan adds no new import anywhere — `watch.py` already imports both `bar` and `github`.
- Do not touch `except OSError, subprocess.TimeoutExpired:` in `github.py`. PEP 758 makes it valid on 3.14 and `ruff format` rewrites the parenthesized form *into* it.
- Coverage floor is 75% (currently ~95%). Do not regress it.
- No test may reach the real `github.resolve_token`. The existing monkeypatches stay.

---

### Task 1: `bar.strip_undisplayable`

A pure function, no I/O, no callers yet. Task 3 wires it up.

**Files:**
- Modify: `src/busyboy/bar.py` (add after `_to_displayable_ascii`, which ends at line 213)
- Test: `tests/test_bar.py` (append at end of file)

**Interfaces:**
- Consumes: nothing.
- Produces: `bar.strip_undisplayable(text: str) -> str`. Returns `text` with every character outside `\x20`-`\x7e` removed and the remaining whitespace collapsed to single spaces, stripped at both ends. May return `""`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bar.py`:

```python
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("CI", "CI"),
        ("🚀 Deploy", "Deploy"),
        ("CI 🚀 Build", "CI Build"),
        ("🚀", ""),
        ("", ""),
        ("  Release  ", "Release"),
    ],
)
def test_undisplayable_characters_are_dropped_rather_than_replaced(text, expected):
    assert bar.strip_undisplayable(text) == expected
```

Note the contrast with the existing `test_a_label_that_is_entirely_non_ascii_is_still_a_valid_non_empty_payload`, which asserts `"日本語"` becomes `"???"`. Both behaviours are correct and deliberate — see the docstring in Step 3.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest tests/test_bar.py::test_undisplayable_characters_are_dropped_rather_than_replaced -v`

Expected: 6 FAILs with `AttributeError: module 'busyboy.bar' has no attribute 'strip_undisplayable'`.

- [ ] **Step 3: Write the implementation**

In `src/busyboy/bar.py`, directly after `_to_displayable_ascii`:

```python
def strip_undisplayable(text: str) -> str:
    """
    Drop every character the display's bitmap fonts cannot render, collapsing the leftover whitespace.

    Unlike `_to_displayable_ascii`, this may return "". It is for text that is
    one *component* of a row rather than a whole row — text where some other
    component already guarantees `TextElement.text`'s `min_length=1`, so
    dropping characters cannot produce an invalid payload. That is what buys
    the nicer rendering: a workflow named "🚀 Deploy" reads as "Deploy" here,
    where one-for-one replacement would leave a stray "?" in front of it.
    Callers must handle the empty result.
    """
    displayable = "".join(character for character in text if "\x20" <= character <= "\x7e")
    return " ".join(displayable.split())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --frozen pytest tests/test_bar.py -v`

Expected: PASS, including all pre-existing `test_bar.py` tests.

- [ ] **Step 5: Full check and commit**

Run: `make test`
Expected: lint clean, all tests pass.

```bash
git add src/busyboy/bar.py tests/test_bar.py
git commit -m "Add bar.strip_undisplayable for row components"
```

---

### Task 2: `Target` holds the resolved workflow

A pure refactor. No behaviour changes, no display output changes, no new tests — the existing suite must pass untouched apart from the fixture construction. If any assertion about *output* needs changing here, something has gone wrong.

**Files:**
- Modify: `src/busyboy/watch.py:39-45` (the `Target` dataclass), `src/busyboy/watch.py:114` (inside `tick`)
- Modify: `src/busyboy/cli.py:207-211` (the `Target` construction in `workflow`)
- Test: `tests/test_watch.py:13-14` (module-level fixture), `tests/test_watch.py` (the `unicode_target` local in `test_a_non_ascii_branch_name_completes_and_draws_instead_of_raising`)

**Interfaces:**
- Consumes: `github.Workflow` (already exists: `id: int`, `name: str`, `path: str`).
- Produces: `watch.Target(repo: github.Repo, branch: str, workflow: github.Workflow)`. The `workflow_id: int` field is gone. Task 3 reads `target.workflow.name`.

- [ ] **Step 1: Update the test fixtures first, and watch them fail**

In `tests/test_watch.py`, replace the module-level target (lines 13-14):

```python
REPO = github.Repo(owner="mb-dot-dev", name="busyboy")
WORKFLOW = github.Workflow(id=42, name="CI", path=".github/workflows/main.yaml")
TARGET = watch.Target(repo=REPO, branch="feature/x", workflow=WORKFLOW)
```

And in `test_a_non_ascii_branch_name_completes_and_draws_instead_of_raising`, replace:

```python
    unicode_target = watch.Target(repo=REPO, branch="feature/café", workflow_id=42)
```

with:

```python
    unicode_target = watch.Target(repo=REPO, branch="feature/café", workflow=WORKFLOW)
```

`RUNS_URL` still points at `.../workflows/42/runs` and stays correct — the id is unchanged, only where it is carried.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest tests/test_watch.py -v`

Expected: collection fails at import time with `TypeError: Target.__init__() got an unexpected keyword argument 'workflow'`. This is a whole-module failure, not a per-test one — that is expected, since `TARGET` is built at module scope.

- [ ] **Step 3: Change the dataclass**

In `src/busyboy/watch.py`, replace the `Target` dataclass:

```python
@dataclasses.dataclass(frozen=True)
class Target:
    """
    What is being watched. Resolved once at startup; none of it changes mid-watch.

    The whole `github.Workflow` is held rather than just its id, so the id used
    to fetch runs and the name shown on the display can only ever come from the
    same `resolve_workflow` call and cannot drift apart.
    """

    repo: github.Repo
    branch: str
    workflow: github.Workflow
```

- [ ] **Step 4: Update the one read of the id**

In `src/busyboy/watch.py`, inside `tick`, replace:

```python
        run = github.latest_run(token, target.repo, target.workflow_id, target.branch)
```

with:

```python
        run = github.latest_run(token, target.repo, target.workflow.id, target.branch)
```

- [ ] **Step 5: Update the CLI construction**

In `src/busyboy/cli.py`, in the `workflow` command body, replace:

```python
    target = watch.Target(
        repo=repo,
        branch=branch or git.current_branch(),
        workflow_id=github.resolve_workflow(github_token, repo, workflow_reference).id,
    )
```

with:

```python
    target = watch.Target(
        repo=repo,
        branch=branch or git.current_branch(),
        workflow=github.resolve_workflow(github_token, repo, workflow_reference),
    )
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run --frozen pytest tests/test_watch.py tests/test_cli.py -v`

Expected: PASS. No output assertion anywhere should have needed touching — behaviour is identical.

This is also what covers "`tick` still fetches runs with the workflow's id": every `responses` registration is keyed on the literal URL `.../actions/workflows/42/runs`, so passing a wrong id (or `None`) produces an unmatched-request failure rather than a silent pass. No new test is needed for it.

- [ ] **Step 7: Full check and commit**

Run: `make test`
Expected: lint clean (including `ty check` — a missed `target.workflow_id` would surface here), all tests pass.

```bash
git add src/busyboy/watch.py src/busyboy/cli.py tests/test_watch.py
git commit -m "Carry the whole resolved workflow on watch.Target"
```

---

### Task 3: Compose the bottom row, and document it

The behaviour change, plus the docstring and CLAUDE.md updates it makes necessary.

**Files:**
- Modify: `src/busyboy/watch.py` (the `render` function)
- Modify: `src/busyboy/bar.py` (the `build_workflow_payload` docstring)
- Modify: `CLAUDE.md` (the `watch.py` module bullet; the `_to_displayable_ascii` gotcha)
- Test: `tests/test_watch.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `bar.strip_undisplayable` (Task 1), `target.workflow.name` (Task 2).
- Produces: `watch.render` returns a `Screen` whose `ref_label` is `f"{ref} {name}"`, or bare `ref` when the stripped name is empty. `Screen`'s fields are unchanged: `repo_label: str`, `ref_label: str`, `icon: bar.IconName`.

- [ ] **Step 1: Update the existing label assertions**

In `tests/test_watch.py`, every `ref_label="#12"` becomes `ref_label="#12 CI"` — there are 7 occurrences (one assertion in `test_an_open_pull_request_is_shown_as_its_number`, one in `test_a_tick_draws_the_current_state`, and five `previous = watch.Screen(...)` fixtures). A `replace_all` edit of the exact string `ref_label="#12"` → `ref_label="#12 CI"` covers all of them.

Two of these are load-bearing rather than cosmetic: `test_an_unchanged_state_is_not_redrawn` passes `previous` and asserts no POST happens, so a stale `"#12"` there would make the screens differ and the test would fail on a spurious redraw — which is exactly the regression the diff exists to prevent.

Then in `test_without_a_pull_request_the_branch_name_is_shown`, replace:

```python
    assert screen.ref_label == "feature/x"
```

with:

```python
    assert screen.ref_label == "feature/x CI"
```

In `tests/test_cli.py`, in `test_watching_a_workflow_draws_and_exits_cleanly`, replace:

```python
    assert elements["ref"]["text"] == "#12"
```

with:

```python
    assert elements["ref"]["text"] == "#12 CI"
```

That one line is the end-to-end check the spec asks for on the CLI side. `"CI"` originates in the `github_bar` fixture's mocked `/actions/workflows` response, so asserting it reaches the draw payload proves the name travelled through `resolve_workflow` → `Target` → `render` → `build_workflow_payload` intact. Inspecting the constructed `Target` directly would prove strictly less.

- [ ] **Step 2: Write the new failing tests**

Append to `tests/test_watch.py`:

```python
def test_an_emoji_in_the_workflow_name_is_dropped_rather_than_replaced():
    target = watch.Target(
        repo=REPO,
        branch="main",
        workflow=github.Workflow(id=42, name="🚀 Deploy", path=".github/workflows/deploy.yaml"),
    )

    screen = watch.render(target, run("completed", "success"), 7)

    assert screen.ref_label == "#7 Deploy"


def test_a_workflow_name_with_nothing_displayable_is_omitted_entirely():
    """The row falls back to exactly the ref, with no orphaned separator."""
    target = watch.Target(
        repo=REPO,
        branch="main",
        workflow=github.Workflow(id=42, name="🚀", path=".github/workflows/deploy.yaml"),
    )

    screen = watch.render(target, run("completed", "success"), 7)

    assert screen.ref_label == "#7"
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run --frozen pytest tests/test_watch.py tests/test_cli.py -v`

Expected: FAILs on the new tests (`assert '#7' == '#7 Deploy'`) and on every assertion updated in Step 1 (`assert '#12' == '#12 CI'`). `test_an_unchanged_state_is_not_redrawn` should also fail, on the spurious POST.

- [ ] **Step 4: Compose the label**

In `src/busyboy/watch.py`, replace `render` in full:

```python
def render(target: Target, run: github.Run | None, pull_request: int | None) -> Screen:
    """
    Turn a fetched run into the three things the display shows.

    The workflow name is stripped rather than sanitized in place, and dropped
    entirely when nothing displayable survives — the ref is always present and
    always ASCII, so the row cannot end up empty either way. See
    `bar.strip_undisplayable`.
    """
    ref = f"#{pull_request}" if pull_request is not None else target.branch
    name = bar.strip_undisplayable(target.workflow.name)
    return Screen(
        repo_label=target.repo.slug,
        ref_label=f"{ref} {name}" if name else ref,
        icon=icon_for(run),
    )
```

`Screen` is deliberately not given a fourth field: the workflow is resolved once at startup and never changes, so the name is a constant inside `ref_label` and the `screen == previous` comparison in `tick` keeps working unchanged.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --frozen pytest tests/test_watch.py tests/test_cli.py -v`

Expected: PASS, all of them.

- [ ] **Step 6: Update the payload builder's docstring**

In `src/busyboy/bar.py`, in `build_workflow_payload`, replace the summary line:

```python
    Build the two-row workflow layout: repository, pull request or branch, and a status icon.
```

with:

```python
    Build the two-row workflow layout: repository on top; pull request or branch and the workflow name below;
    a status icon to their left.
```

No code in `bar.py` changes — it receives a `ref_label` string and does not care what is in it.

- [ ] **Step 7: Update CLAUDE.md**

Two edits.

First, in the `src/busyboy/watch.py` bullet under Architecture, append to the existing sentence about `tick` and `watch` — after "...knows nothing about Click or argv — `cli.py` builds its `Target` and passes it in." add:

```markdown
  `render` composes the bottom row as the ref (`#123` for an open pull request, otherwise the branch) followed
  by the workflow's display name, so two workflows in the same repository on the same branch are
  distinguishable on the bar.
```

Second, under Gotchas, extend the note that currently begins "**`bar._to_displayable_ascii` sanitizes the workflow rows to `?`; `build_text_payload` still rejects non-ASCII outright.**" by appending this paragraph to it:

```markdown
`bar.strip_undisplayable` is a third point on that same spectrum, and the three are not redundant. Reject
outright (`build_text_payload`) for text a human typed a moment ago and can retype. Replace one-for-one
(`_to_displayable_ascii`) for a whole row, where dropping characters could empty it and trip
`TextElement.text`'s `min_length=1` mid-poll-loop, killing the watch over a value nobody typed. Drop and
collapse (`strip_undisplayable`) for one *component* of a row — the workflow name — where the ref beside it
already guarantees the row is non-empty, so a name like "🚀 Deploy" can read as "Deploy" instead of
"? Deploy". Collapsing the two private-looking helpers into one would either put stray `?`s back in front of
emoji-prefixed workflow names or reintroduce the empty-row crash. Keep all three.
```

- [ ] **Step 8: Full check and commit**

Run: `make test`
Expected: lint clean, all tests pass. Confirm coverage has not regressed with `make coverage`.

```bash
git add src/busyboy/watch.py src/busyboy/bar.py CLAUDE.md tests/test_watch.py tests/test_cli.py
git commit -m "Show the workflow name in the bottom row"
```

---

## Manual verification (optional, needs a real bar)

The spec's claims about scroll behaviour were measured on hardware, and the new row two is longer than the old one. If a bar is available:

```bash
source ~/.zshrc && busybarenv
uv run --frozen busyboy gh workflow CI
```

Confirm the bottom row reads `<ref> CI` and scrolls as one line. Never print or commit the token value. `tools/capture_screen.py` can grab a frame if a pixel-level look is wanted.

This is not a gate on the plan — the automated suite covers the logic — but it is the only way to see the composed row at its real width.
