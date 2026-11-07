# claude-artifacts-skills

A small, portable collection of Claude skills — usable inside Claude Code **and** in
any other skill-aware harness (or standalone), with **no configuration**. Each
top-level folder is a self-contained skill: a `SKILL.md` with YAML frontmatter
(`name` + `description`) that a harness reads to load it, plus any `references/` and
`scripts/` it needs.

## Skills in here

### `publish-artifact/` — Claude Artifacts from any shell or harness
Everything Claude Code's built-in Artifact, ArtifactData and ArtifactComments tools do,
as one stdlib-only CLI (`artifact.py`) over the reverse-engineered "frame" API, with no
LLM turn: publish and update pages (supporting files, capabilities, contract pins,
create from a type), read pages and files, list/delete/pin/retitle, version history,
the asset store, the artifact database (get/list/query/set/update/str_replace/delete/
batch with `if_version`), user profiles, comment threads, and artifact types. Every
command prints JSON. The token is read only from `CLAUDE_CODE_OAUTH_TOKEN`.
```bash
export CLAUDE_CODE_OAUTH_TOKEN=...      # e.g. from `claude setup-token`
A=publish-artifact/artifact.py
python3 $A publish page.html --title "My Page" --icon chart
python3 $A db query "$URL" logs --where date == 2026-11-07
python3 $A comments read "$URL"
```
The older `publish_artifact.py` (publish/list only) still works. See
`publish-artifact/SKILL.md` for every command, and `publish-artifact/references/`
(endpoint table with confidence levels, auth, and the extracted `artifact-design` /
`artifact-capabilities` guidance). Offline tests:
`cd publish-artifact && python3 -m unittest discover -s tests`.

### `dataviz/` — data-visualization design system
Anthropic's built-in `dataviz` skill, extracted verbatim: a design-system-agnostic
procedure for any chart/graph/dashboard/SVG, with a **runnable, dependency-free
palette validator** (both Node and Python) so color choices are checked, not
eyeballed.
```bash
# validate a categorical palette (either language; identical checks)
node   dataviz/scripts/validate_palette.js "#4c78a8,#f58518,#54a24b" --mode light
python3 dataviz/scripts/validate_palette.py "#4c78a8,#f58518,#54a24b" --mode light
```
See `dataviz/SKILL.md` and `dataviz/references/`.

## How harnesses load these (no configuration needed)

Skill-aware harnesses discover skills by scanning a skills directory and reading each
`SKILL.md`'s frontmatter. To make these available, put the skill folder where your
harness looks — the standard locations:

```bash
# Claude Code (user-level skills):
ln -s "$PWD/publish-artifact" ~/.claude/skills/publish-artifact
# (dataviz is already built into Claude Code, so don't shadow it there —
#  copy dataviz/ into OTHER harnesses' skill dirs instead.)

# Any other harness: drop or symlink the folder into that harness's skills path.
```

That's the whole setup — no config files, no registration. The frontmatter is the
contract; the folder is the unit.

## Using standalone (no harness at all)

Everything runs directly:
- `publish-artifact`: only `artifact.py` (or the older `publish_artifact.py`) + an OAuth
  token are needed at runtime.
- `dataviz`: read the `SKILL.md`/`references/` as guidance; run the validator scripts directly.

## Provenance & disclaimer

`dataviz`, and the `artifact-design` / `artifact-capabilities` docs under
`publish-artifact/references/`, are extracted verbatim from the Claude Code CLI
(v2.1.212) for offline/portable use. `publish-artifact` is an unofficial,
reverse-engineered tool for personal/scripted use — not affiliated with or endorsed
by Anthropic; the underlying private API may change without notice. Its endpoint map
was re-derived from v2.1.319; most endpoints are inferred from the binary rather than
verified live (see `publish-artifact/references/artifact-protocol.md`).

## Security

No credentials are stored in this repo. Treat any OAuth token you generate like a
password — the `.gitignore` blocks common credential filenames; never paste a token
into tracked files.

`artifact.py` reads the token only from `CLAUDE_CODE_OAUTH_TOKEN`, sends it only to
`api.anthropic.com` (or a loopback host for tests; any other base needs
`ARTIFACT_ALLOW_CUSTOM_BASE=1`), never follows redirects with it, and redacts token-like
fields (such as an artifact's asset token) from its output. Deleting an artifact or an
asset needs an explicit `--yes`. It has no sharing or permission commands.
