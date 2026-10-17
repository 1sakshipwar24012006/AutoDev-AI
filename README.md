# Autonomous Coding Workflow Agent

An agent that turns a GitHub issue into a **tested, self-reviewed pull request**, using the Claude API for
reasoning and plain `git` / `gh` for everything that touches your repository.

```
issue ─► analyse repo ─► baseline tests ─► AI plan ─► new branch
                                                        │
        ┌───────────────────────────────────────────────┘
        ▼
   AI generates edits ─► safe patch ─► run tests ─► AI self-review ─► commit ─► push + open PR
        ▲                   │ fail        │ fail         │ reject        (never merges, never main)
        └───────── retry with the failure fed back (max N attempts, every attempt rolled back) ──┘
```

Built with Python 3.10+, the Anthropic SDK, Pydantic v2, Git, the GitHub CLI and Pytest.

## Safety guarantees

| Concern | What the agent does |
|---|---|
| Main branch | Always works on a new `agent/...` branch. `push` refuses `main`, `master`, `develop`, `release/*`, … and always uses an explicit refspec (no `--force`). |
| Merging | There is no merge code path. PRs are opened (optionally as drafts) for a human to review. |
| Your files | Only files in the change set are staged (`git add -- <paths>`, never `-A`). A dirty tracked tree is refused unless `--allow-dirty`. Your original branch is restored afterwards. |
| Bad AI output | Every edit is validated in memory first; the whole patch is applied atomically and rolled back on any failure or failing test. |
| Path safety | Absolute paths, `..`, symlink escapes, Windows reserved names, `.git/`, `.env*`, keys/certs and `.github/workflows/*` are all rejected. Existing test files cannot be edited unless `--allow-edit-existing-tests` (stops "make the tests pass by changing them"). |
| Secrets | `ANTHROPIC_API_KEY` is read from the environment / a local `.env` (launch dir or `--env-file`, never the target repo), held as a `SecretStr`, never forwarded to child processes, and scrubbed from logs, reports and command output. Secret-looking files are never sent to the model. |
| Subprocesses | argv lists only (no shell), resolved executables, timeouts with process-tree kill, bounded output. Tests run with all credential-like environment variables removed. Test commands are limited to `python` / `pytest`. |
| Prompt injection | Issue text and repo content are wrapped in tags and declared untrusted; the path/command policies above are enforced in code regardless of what the model says. |
| Dry run | `--dry-run` works on a throw-away copy: no branch, commit, push or PR, and your repository is not modified — but tests really run against the proposed patch. |
| Mock mode | `--mock` replays canned responses: no API key, no network, no cost. Mock results can never be pushed (`--mock` + `--create-pr` is rejected). |

> **Important:** the generated code is executed when tests run. Even with the protections above, run the agent
> against repositories you trust, ideally inside a container or VM, and always review the diff and PR yourself.

## Requirements

* Python 3.10+ and Git
* [GitHub CLI](https://cli.github.com/) (`gh`) — only for fetching issues by number and opening PRs
* An Anthropic API key — only for real (non-mock) runs

## Setup — Linux / macOS

```bash
git clone <this-repo> autonomous-coding-workflow-agent && cd autonomous-coding-workflow-agent
./scripts/setup.sh                 # venv + dependencies + tests   (or do it manually:)

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install -e .
python -m pytest                   # unit tests, no API key needed

# GitHub CLI (Debian/Ubuntu shown; see https://github.com/cli/cli#installation for others)
sudo apt install gh        # or: brew install gh
gh auth login

cp .env.example .env       # then edit .env and set ANTHROPIC_API_KEY
# or: export ANTHROPIC_API_KEY="sk-ant-..."
```

## Setup — Windows PowerShell

```powershell
git clone <this-repo> autonomous-coding-workflow-agent; cd autonomous-coding-workflow-agent
.\scripts\setup.ps1                # venv + dependencies + tests   (or do it manually:)

py -3 -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass   # only if activation is blocked
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt; pip install -e .
python -m pytest

winget install --id GitHub.cli     # then restart the terminal
gh auth login

Copy-Item .env.example .env        # then edit .env and set ANTHROPIC_API_KEY
# or for this session only:
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

## Quick start (offline, free)

```bash
workflow-agent demo                 # buggy sample app -> branch + commit in a temp git repo
workflow-agent demo --dry-run       # same, but nothing is committed
python -m workflow_agent demo       # equivalent if the console script is not on PATH
```

The demo copies `sample_app/` (a tiny shop library with two bugs: `apply_discount` returns the discount
instead of the discounted price, and `average_price([])` divides by zero) into a temp repo, then runs the full
workflow with the bundled mock fixture. The first mock attempt is deliberately incomplete, so you can watch the
retry loop: attempt 1 fails the tests and is rolled back, attempt 2 passes, is "reviewed", and is committed on
`agent/...`. The run report, `changes.patch` and `pr_body.md` are written next to the demo repo.

## Using it on a real repository

```bash
# 1. See what context the model would receive (no API calls)
workflow-agent analyze --repo /path/to/repo --issue-text "crash when cart is empty"

# 2. Rehearse safely: real API calls, real tests, nothing changed in your repo
workflow-agent run --repo /path/to/repo --issue 42 --dry-run

# 3. Fix locally: creates agent/issue-42-<slug> with one commit, no push
workflow-agent run --repo /path/to/repo --issue 42

# 4. Push the branch and open a (draft) pull request for review
workflow-agent run --repo /path/to/repo --issue 42 --create-pr --draft

# Work from a local issue file instead of GitHub (Markdown or JSON)
workflow-agent run --repo . --issue-file issue.md --dry-run
```

PowerShell is identical (use backticks for line continuation).

Useful options: `--max-attempts N`, `--test-command "python -m pytest -q"`, `--base BRANCH`, `--branch NAME`,
`--github-repo owner/name`, `--allow-dirty`, `--allow-edit-existing-tests`, `--output-dir DIR`, `--env-file FILE`,
`-v`. Exit codes: `0` success, `1` workflow failed (no changes left behind), `2` usage/configuration error.

Each run writes `report.json`, `changes.patch` (apply with `git apply`) and, on success, `pr_body.md` to
`agent_runs/<timestamp>-issue-N/`. Failed runs keep `last_attempt_rejected.patch` for inspection.

## Configuration

Environment variables (or `.env`, see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | Required for real runs |
| `AGENT_MODEL` | `claude-sonnet-5-5` | Claude model for plan/generate/review |
| `AGENT_MAX_TOKENS` | `8000` | Max output tokens per call |
| `AGENT_MAX_ATTEMPTS` | `3` | Generate/test/review attempts |
| `AGENT_TEST_COMMAND` | `python -m pytest -q -p no:cacheprovider` | argv-style test command (`python`/`pytest` only) |
| `AGENT_TEST_TIMEOUT` | `300` | Seconds before tests are killed |
| `AGENT_MAX_CONTEXT_CHARS` | `60000` | Repository context budget |
| `AGENT_GITHUB_REPO` | inferred | `owner/name` passed to `gh` |
| `AGENT_OUTPUT_DIR` | `agent_runs` | Reports/patches location |

A bare `python` in the test command means *the interpreter running the agent*. To test a project that has its
own virtualenv, give the absolute path to that interpreter, e.g. `--test-command "/path/to/venv/bin/python -m pytest"`.

## How the model's output is constrained

Claude is forced to answer through a tool whose JSON schema comes from a Pydantic model
(`Plan`, `ChangeSet`, `Review`), and the result is re-validated (one automatic retry with the validation error).
Edits are one of three safe operations: `replace_text` (the `old_text` must match exactly once), `create`
(fails if the file exists) or `overwrite`. There is no delete operation and no arbitrary command execution.

## Mock mode and fixtures

`--mock` loads a JSON file mapping a stage (`plan`, `codegen`, `review`) to one response or a list of responses
that are replayed in order (the last one repeats). See `src/workflow_agent/fixtures/sample_app_fix.json`, and
pass your own with `--mock-fixture`. This is how the whole test suite runs without paid API access.

## Project layout

```
src/workflow_agent/
  cli.py              argparse CLI: run | demo | analyze
  workflow.py         orchestration, retry loop, branch/commit/PR steps, cleanup
  config.py           settings, .env loading, SecretStr API key
  security.py         path/branch/test-command policy, secret redaction
  shell.py            safe subprocess runner (no shell, scrubbed env, timeouts)
  repo_analyzer.py    file listing, secret/binary filtering, relevance ranking
  llm.py              ClaudeClient (tool-use structured output) + MockLLMClient
  prompts.py          system/user prompts (untrusted content is tagged)
  planner.py codegen.py reviewer.py    the three AI stages
  patcher.py          atomic, reversible edit application + git-compatible diffs
  test_executor.py    runs tests with credentials removed from the environment
  git_ops.py          branches, explicit-path commits, guarded push
  github_client.py    `gh` wrapper: fetch issue, create PR (no merge)
  reporting.py        PR title/body and run artefacts
  demo.py issues.py   demo repo builder, local issue loader
sample_app/           buggy demo application (+ failing tests, issue.md)
tests/                unit + end-to-end tests (no API key, no network)
scripts/              setup.sh / setup.ps1
```

## Running the tests

```bash
python -m pytest            # ~110 tests, no API key or network needed
```

The suite covers path/branch/command policy, secret redaction, the subprocess runner (including timeouts and
environment scrubbing), the patcher (atomicity, rollback, CRLF, symlinks, git-apply validity), the Claude client
with a fake SDK, the `gh` wrapper with a fake runner, git operations on real temporary repositories, and the
whole workflow end to end: success with retry, dry-run, rollback on failure, review rejection, policy
violations, dirty trees and the PR flow against a local bare remote.

## Limitations

* Files larger than `max_file_bytes` (50 KB) are not shown to the model, so they cannot be edited with `replace_text`.
* Repository context is selected heuristically (keyword/path relevance), not by full code intelligence.
* Only Python/pytest test commands are supported out of the box.
* The self-review is another model pass, not a guarantee of correctness. Review every PR.

## Troubleshooting

* **`ANTHROPIC_API_KEY is not set`** – export it, use `.env` in the directory you run from, or pass `--env-file`.
* **`executable not found on PATH: 'gh'`** – install the GitHub CLI and run `gh auth login`.
* **`working tree has uncommitted changes`** – commit or stash tracked changes, or pass `--allow-dirty`.
* **`Please tell me who you are`** – set `git config user.name` / `user.email`.
* **PowerShell blocks `Activate.ps1`** – `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`.
* **Model output truncated** – increase `AGENT_MAX_TOKENS` or narrow the issue.
#   A u t o D e v - A I  
 