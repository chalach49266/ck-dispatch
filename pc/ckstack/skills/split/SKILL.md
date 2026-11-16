---
name: split
description: Run different parts of one ask at the same time on different model families, then merge them and do one check. Word "split".
disable-model-invocation: true
---

# split

**Word**: `split`. **Size** L unless chalach gave one (L: 3 runs live at once, 6 runs).
S or M: the parts run one after another, unless chalach's words ask for parallel
(`--parallel N --because "<their words>"`).
Example: Sol writes the counter code, Grok reads the SPCM datasheet, Opus writes the wiki text.

## Steps
1. Plan card `~/PhD_local/agent_runs/<name>_plan.md`: the parts; who does each (by lane); which files each part owns
   (no two parts share a file); the merge; the one check; a block diagram; the line
   `budget: runs=6 parallel=3 reviews=2 fixes=2 wall_min=180` (change it as needed). chalach OKs it.
2. Open: `ck-run ask open --words "<chalach verbatim>" --size L --title "<the goal in your words>" --pattern split --models sol,grok,opus --project <name> --plan ~/PhD_local/agent_runs/<name>_plan.md`
3. One brief per part: `~/PhD_local/agent_runs/<ask>-part<n>_prompt.md`, each with "Do only this" for that part.
4. Launch each part (from its task folder):
   `ck-run launch <ask>-part<n> run --model <alias> --workspace devfab|nv|washu --title "<part title>" --ask <ask> [--write-scope <its folder>] [--claim <file>]`
   Exit 6 (busy) means two parts claimed the same path: fix the plan, not the claim.
5. Merge: one run joins the parts (or you, when it is small), then one check with a reason (`ck-mode` section 5).
6. A part that fails: retry that part only (`--parent <its run>`). Never rerun the whole split.
7. Close: `ck-run ask close <ask> --done yes|partial|no`.

## Never
- Split one small job into parts just to use more agents.
- Let two parts edit the same file.
