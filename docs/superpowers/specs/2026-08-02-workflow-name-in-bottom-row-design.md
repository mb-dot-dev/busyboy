# Workflow name in the bottom row

## Problem

`busyboy gh workflow` shows two rows: the repository slug on top, and the pull request number or branch name
below. Nothing on the display names the workflow being watched. A repository with several workflows — CI,
release, a nightly job — renders identically for all of them, so a bar showing `mb-dot-dev/busyboy` / `#7` and a
red icon does not say which workflow failed.

## Solution

Append the workflow's display name to the bottom row, after the pull request number or branch name.

```
row 1:  mb-dot-dev/busyboy
row 2:  #7 CI
```

On a branch with no open pull request:

```
row 1:  mb-dot-dev/busyboy
row 2:  main CI
```

### Which name

The workflow's display name — its `name:` field, as GitHub's own Actions UI shows it. `github.Workflow.name`
already carries it, and `github.resolve_workflow` already returns a fully populated `Workflow`; today `cli.py`
discards everything but `.id`.

Rejected alternatives: the filename (`main.yaml`) carries a redundant extension and names the file rather than
the workflow; echoing the `WORKFLOW` argument verbatim would put a bare numeric id on the display whenever
someone watches by id.

### Separator

A single space. The bottom row is a 54px column in the 7-row `small` font, so every character costs scroll
time, and the two parts already read as distinct tokens — a `#`-prefixed number or a branch name, then a name.
A middle dot is not an option: the display's fonts are bitmap ASCII, and `bar._to_displayable_ascii` would
render it as `?`.

### Emoji in workflow names

GitHub permits Unicode in workflow names and prefixing with an emoji is a common convention (`🚀 Deploy`).
`bar._to_displayable_ascii`, which every row passes through, replaces each non-ASCII character one-for-one with
`?`, so such a name would reach the display as `#7 ? Deploy`.

That one-for-one replacement exists because `TextElement.text` requires `min_length=1`: dropping characters
could empty a row whose entire content was non-ASCII, and a validation error mid-poll-loop would kill the
watch. It stays as-is for row one and for the ref.

The workflow name is different — it is one component of a row whose other component (the ref) is always
present and always ASCII, so the non-empty guarantee does not depend on it. It therefore gets its own
treatment: non-ASCII characters are **dropped**, not replaced, and the leftover whitespace is collapsed.

| workflow name | bottom row (PR #7) |
|---|---|
| `CI` | `#7 CI` |
| `🚀 Deploy` | `#7 Deploy` |
| `CI 🚀 Build` | `#7 CI Build` |
| `🚀` | `#7` |

A name that strips to nothing is omitted entirely, along with its separating space — the row is then exactly
what it renders today.

### No truncation

A long branch name plus a long workflow name will scroll for a while. That is already true of row one, which
scrolls any repository slug wider than the text column, and the scroll rate is unchanged. Truncating would
introduce a length budget, a decision about which part to cut, and an ellipsis convention — none of which the
display needs to be readable.

## Changes

### `src/busyboy/bar.py`

Add a public sibling to `_to_displayable_ascii`:

```python
def strip_undisplayable(text: str) -> str:
    """
    Drop every character the display's bitmap fonts cannot render, collapsing the leftover whitespace.

    Unlike `_to_displayable_ascii`, this may return "" — it is for text that is
    one *component* of a row rather than a whole row, where some other component
    already guarantees `TextElement.text`'s min_length=1. Callers must handle the
    empty result.
    """
    return " ".join("".join(character for character in text if "\x20" <= character <= "\x7e").split())
```

It lives in `bar.py` because which characters the display can render is BUSY Bar knowledge, and `bar.py` is
where that lives. `watch.py` already imports `bar`, so this adds no import edge.

Nothing else in `bar.py` changes. `build_workflow_payload` takes a `ref_label` string and does not care what is
in it.

### `src/busyboy/watch.py`

`Target` holds the resolved workflow rather than just its id:

```python
@dataclasses.dataclass(frozen=True)
class Target:
    repo: github.Repo
    branch: str
    workflow: github.Workflow
```

This keeps the id and the name from drifting apart — they can only ever come from the same `resolve_workflow`
call — and removes an unpack at the one call site. `Workflow.path` goes unused; that is the cost of holding a
cohesive value rather than two fields that must be kept consistent by hand.

`tick` passes `target.workflow.id` where it passed `target.workflow_id`.

`render` composes the bottom row:

```python
def render(target: Target, run: github.Run | None, pull_request: int | None) -> Screen:
    """Turn a fetched run into the three things the display shows."""
    ref = f"#{pull_request}" if pull_request is not None else target.branch
    name = bar.strip_undisplayable(target.workflow.name)
    return Screen(
        repo_label=target.repo.slug,
        ref_label=f"{ref} {name}" if name else ref,
        icon=icon_for(run),
    )
```

`Screen` gains no field. The workflow name is resolved once at startup and never changes, so it is a constant
inside `ref_label`. The `screen == previous` comparison in `tick` — which is load-bearing, since an
unconditional redraw restarts the scroll animation — keeps working untouched.

### `src/busyboy/cli.py`

Pass the resolved workflow straight through:

```python
target = watch.Target(
    repo=repo,
    branch=branch or git.current_branch(),
    workflow=github.resolve_workflow(github_token, repo, workflow_reference),
)
```

No behavioural change to option parsing, error handling, or exit codes.

### Docstring and `CLAUDE.md`

`bar.build_workflow_payload`'s docstring says the layout is "repository, pull request or branch, and a status
icon". Add the workflow name.

CLAUDE.md's two mentions of the layout are both about element structure, not row content, and stay accurate as
written. Two additions are needed:

- The `watch.py` module bullet gains a sentence: `render` composes the bottom row as the ref followed by the
  workflow name.
- The Gotchas section already carries a note on why `bar._to_displayable_ascii` sanitizes while
  `build_text_payload` rejects. `strip_undisplayable` makes that a three-way distinction, and a later reader
  looking at two near-identical private helpers is exactly who would "simplify" one into the other. Extend that
  note: replace-in-place for a whole row, drop-and-collapse for a component of a row whose non-emptiness some
  other component guarantees, reject outright for text a human typed a moment ago.

## Error handling

Unchanged. No new failure mode is introduced: the workflow name is already fetched during startup resolution,
inside the existing `resolve_workflow` call whose failures already print one line and exit 1. Nothing in the
poll loop makes a new request or can raise where it could not before.

## Testing

`tests/test_watch.py`

- Update every `Target` construction to pass a `github.Workflow` instead of `workflow_id`.
- `render` returns `"#7 CI"` when a pull request is open.
- `render` returns `"main CI"` when there is none.
- `render` strips an emoji from the name: `"🚀 Deploy"` gives `"#7 Deploy"`.
- `render` omits a name that strips to nothing: `"🚀"` gives `"#7"`, with no trailing space.
- `tick` still calls `latest_run` with the workflow's id.
- A tick whose run state is unchanged still returns `previous` without drawing — the diff must survive the
  `ref_label` change.

`tests/test_bar.py`

- `strip_undisplayable` drops non-ASCII, collapses the resulting whitespace, and returns `""` for wholly
  non-ASCII input.

`tests/test_cli.py`

- The `resolve_workflow` monkeypatch already returns a `Workflow`; assert the `Target` it produces carries that
  whole object.

No test may reach the real `github.resolve_token` — the existing monkeypatch requirement is unchanged.
