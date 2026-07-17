# claude-artifacts-skills

A small, portable collection of Claude skills — usable inside Claude Code **and** in
any other skill-aware harness (or standalone), with **no configuration**. Each
top-level folder is a self-contained skill: a `SKILL.md` with YAML frontmatter
(`name` + `description`) that a harness reads to load it, plus any `references/` and
`scripts/` it needs.

## Skills in here

### `publish-artifact/` — publish Claude Artifacts from the CLI
Create/update/list [Claude Artifacts](https://claude.ai) via the reverse-engineered
"frame" deploy API, with no Artifact tool and no LLM turn. Stateless, stdlib-only.
```bash
export CLAUDE_CODE_OAUTH_TOKEN="$(claude setup-token)"
python3 publish-artifact/publish_artifact.py page.html --title "My Page" --favicon "📊"
```
See `publish-artifact/SKILL.md` and `publish-artifact/references/` (auth, protocol,
and the extracted `artifact-design` / `artifact-capabilities` guidance).

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
- `publish-artifact`: only `publish_artifact.py` + an OAuth token are needed at runtime.
- `dataviz`: read the `SKILL.md`/`references/` as guidance; run the validator scripts directly.

## Provenance & disclaimer

`dataviz`, and the `artifact-design` / `artifact-capabilities` docs under
`publish-artifact/references/`, are extracted verbatim from the Claude Code CLI
(v2.1.212) for offline/portable use. `publish-artifact` is an unofficial,
reverse-engineered tool for personal/scripted use — not affiliated with or endorsed
by Anthropic; the underlying private API may change without notice.

## Security

No credentials are stored in this repo. Treat any OAuth token you generate like a
password — the `.gitignore` blocks common credential filenames; never paste a token
into tracked files.
