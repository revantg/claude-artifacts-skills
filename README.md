# claude-artifacts-skills

Publish [Claude Artifacts](https://claude.ai) from the command line — with **no
Claude Code Artifact tool, no LLM turn, and no local install or login required** —
using a single stdlib-only Python script. Reverse-engineered from the Claude Code
CLI binary; artifacts are internally called *frames*.

## Layout

```
publish-artifact/
├── publish_artifact.py          # the tool (Python 3 stdlib only)
├── SKILL.md                     # Claude Code skill entry doc + quick start
└── references/
    ├── auth.md                  # get a token in any harness/CI; refresh; safe handling
    ├── artifact-protocol.md     # the frame API: endpoints, headers, limits (re-derivable)
    ├── artifact-design.md       # Anthropic's built-in artifact-design guidance (extracted)
    └── artifact-capabilities/   # runtime capabilities: window.claude.mcp / downloads
```

## Quick start

```bash
# 1. get a token once (on any machine that has `claude` logged in)
export CLAUDE_CODE_OAUTH_TOKEN="$(claude setup-token)"

# 2. publish
python3 publish-artifact/publish_artifact.py page.html \
  --title "My Page" --favicon "📊" --description "A demo"
# -> prints https://claude.ai/code/artifact/<slug>

# update in place (same URL)
python3 publish-artifact/publish_artifact.py page.html --slug <slug> --force

# list your artifacts
python3 publish-artifact/publish_artifact.py --list
```

## Stateless / portable

The only state is the OAuth token. Supply it via `--token`, the
`$CLAUDE_CODE_OAUTH_TOKEN` env var, or a local `~/.claude/.credentials.json` login
(auto-refreshed). At runtime only `publish_artifact.py` and the page are needed —
copy them into any harness, container, or CI and it works. See
[`publish-artifact/references/auth.md`](publish-artifact/references/auth.md).

## Use as a Claude Code skill

Symlink it into your skills dir to invoke it as `/publish-artifact`:

```bash
ln -s "$PWD/publish-artifact" ~/.claude/skills/publish-artifact
```

## Constraints (parity with the app)

- Fully self-contained page (strict CSP blocks all external hosts — inline CSS/JS, data-URI assets).
- Max rendered page **16 MB**; content may not contain `</script`.
- Artifacts start **private** to the owner.

## Security

No credentials are stored in this repo. Treat any token you generate like a
password — the `.gitignore` blocks common credential filenames, but never paste a
token into tracked files.

## Note

This is an unofficial, reverse-engineered tool built for personal/scripted use; it
is not affiliated with or endorsed by Anthropic, and the underlying private API may
change without notice.
