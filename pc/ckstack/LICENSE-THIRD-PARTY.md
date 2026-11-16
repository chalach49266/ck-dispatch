# Third-party credits

ckstack is chalach's own rulebook. It adapts ideas, not code, from the projects below. The text in ckstack is
rewritten in our own words for chalach's lab work; a few short phrases are kept. Every source here uses the MIT
License (full text at the end; each source keeps its own copyright line).

## pstack
- Author: Lauren Tan. Copyright (c) 2026 Lauren Tan. MIT License.
- Source: https://github.com/cursor/plugins/tree/main/pstack (read from a local copy, version 0.15.36).
- Ideas used, and where they went:
  - One router skill in front of playbooks (`poteto-mode`) -> `ck-mode/SKILL.md`.
  - Principles as short rules with "when it applies" -> `principles/PRINCIPLES.md` (explain the number, prove it
    works, fix root causes, subtract before you add, encode lessons in structure, sequence verifiable units).
  - Restate the goal in your own words before work -> the start card title.
  - recall and session pickup -> `skills/recall`, `playbooks/pickup.md`. ketchup -> `skills/ketchup`.
  - why and teach -> `skills/why`, `skills/teach`.
  - swarm and arena (parallel workers; name the pick rule first; one report) -> `skills/race`, `skills/split`.
  - interrogate (only findings proven by a run survive) -> `skills/panel`, principle 7.
  - reflect and correct (fix a repeated mistake at the highest level; keep a rule-to-enforcer table) ->
    `skills/reflect`, `skills/correct`, the table in `README.md`.
  - sharpen (a deeper task, not a bigger one) -> `skills/sharpen`. bro and unslop (plain words) -> `skills/plain`.
  - hillclimb, the decision log (show-me-your-work) and the overnight contract (pstack guide) ->
    `playbooks/design-loop.md`.
  - A models file per role (`/setup-pstack`) -> the lanes in `ck-dispatch/config/models.json` and `models.md`.

## Cursor plugins repository
- Author: Cursor. Copyright (c) 2026 Cursor. MIT License. Source: https://github.com/cursor/plugins
- orchestrate: pass the user's goal through without growing it -> "Do only this" in every brief; a ladder of proof
  levels -> `verification`; the andon stop switch -> `ck-run andon` after exit 4 or 5.
- cursor-team-kit (manifest author Eric Zakariasson): verify-this (baseline and treatment, one verdict) ->
  `skills/verify`; control-ui and control-cli (drive the real UI or CLI as proof) -> the click-through check in
  `playbooks/lab-code.md`; what-did-i-get-done and weekly-review -> `skills/ketchup`, `skills/reflect`.
- advisor: consult a stronger model only at a few checkpoints (a big decision, stuck twice, before done) -> the
  escalation ladder in `ck-mode`.
- thermos: parallel reviewers with different lenses -> `skills/panel`.
- teaching: learning retrospectives -> the "You should know" lines and the weekly list in `skills/teach`.
- continual-learning (manifest author Eric Zakariasson): learn from past sessions -> `skills/reflect`.
- cli-for-agent (manifest author Eric Zakariasson): commands that are safe to rerun, errors that show the fix ->
  `skills/correct`.

## dyl-stack
- Author: Dylan Gattey. Copyright (c) 2026 Dylan Gattey. MIT License. Source: the same repository (`dyl-stack/`).
- The Algorithm (question the requirement, delete, then optimize) -> principle 10, subtract before you add.

## MIT License

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
