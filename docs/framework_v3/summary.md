# Step 8 (2026-11-02): Framework v3 docs, all in ck-dispatch/docs/framework_v3/ (not committed, not pushed; nothing else edited)
- AI_Agent_Delegation_Framework_v3.md: CK = orchestrator only; 4 agents + routing table (route.py); ck-run flow; guards + D1–D4; 10-line summary rule; sessions/resume; steps 0–7 verified; 12 open items; mermaid; "changed from v2" table.
- framework_v3_onepager.html: letter portrait; SVG flow; 4 agent cards; safety box; done list (43/43, 4 smoke runs ok, codex cross-review); open items; glossary. Tags and SVG syntax checked.
- NOT DONE: framework_v3_onepager.pdf. Headless Chrome needed a permission this session could not grant, so the 1-page check was not run. The HTML has a fit-to-one-page script as a backstop.
- To finish from the Grok Bot shell: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless --print-to-pdf=$HOME/PhD_local/ck-dispatch/docs/framework_v3/framework_v3_onepager.pdf --no-pdf-header-footer $HOME/PhD_local/ck-dispatch/docs/framework_v3/framework_v3_onepager.html
  then: /opt/anaconda3/bin/pdfinfo $HOME/PhD_local/ck-dispatch/docs/framework_v3/framework_v3_onepager.pdf | grep Pages (expect 1).
- Found in the ledger: codex ran live via ck-run (3 read runs + 1 review, all ok, so --output-schema works), but the codex-prompt skill still calls it unchecked.
- Open: codex resume -s flag; AQS/ is raw; attribution email; blind review and accept records not done; copy v3 to the PhD folder (D3).
- Update 2026-11-03: AQS/ is writable in nv (01a7f01), and the cursor/agy post-run review no longer flags nv AQS/ edits inside the task folder (AQS/ from other workspaces is still raw).
