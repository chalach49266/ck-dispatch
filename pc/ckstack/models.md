# Models and lanes (the human view)

The source of truth is `~/PhD_local/ck-dispatch/config/models.json`. `ck-run models` prints it as a table. If this
page and that file disagree, the file wins; fix this page.

## The fleet
| Alias | Agent CLI | Model | Effort | Tier | Family |
|---|---|---|---|---|---|
| `sol` | codex | `gpt-6.1-sol` | high | strong | OpenAI |
| `sol-fast` | codex | `gpt-6.1-sol` | low | fast | OpenAI |
| `astra` | codex | `gpt-6-astra` | (none) | top | OpenAI |
| `opus` | claude | Opus 5.5 (`opus`) | high | strong | Anthropic |
| `opus-max` | claude | Opus 5.5 (`opus`) | xhigh | top | Anthropic |
| `sonnet` | claude | Sonnet 5.5 (`sonnet`) | medium | fast | Anthropic |
| `haiku` | claude | Haiku 5.5 (`haiku`) | (none) | fast | Anthropic |
| `grok` | cursor (`agent`) | Grok 4.7 (`grok-4.7-high`) | (none) | strong | xAI |
| `composer` | cursor (`agent`) | Composer 2.5 (`composer-2.5`) | (none) | fast | Cursor |
| `gemini` | agy | Gemini 3.8 Flash (agy's default) | (none) | fast | Google |

How ck-run passes them (ck-run adds these flags; nobody else may): claude `--model` and `--effort`; codex `-m` and,
when an effort is set, `-c model_reasoning_effort="<effort>"`; cursor `--model <slug>`; agy has no model flag.
The effort names each CLI accepts are checked and listed under `_verified` in `models.json`. A value that could not
be checked is marked TODO(verify) there (for codex, ck-run then sends no effort flag).

## Tiers
| Agent | fast | strong | top |
|---|---|---|---|
| claude | sonnet | opus | opus-max |
| codex | sol-fast | sol | astra |
| cursor | composer | grok | grok |
| agy | gemini | gemini | gemini |

- The ask's size sets the tier: S fast, M strong, L strong. No ask: strong.
- `--model <alias>` picks the model and implies the agent. `--tier fast|strong|top` picks a tier for `--agent`.
- Top tier only in a race (`race_default`: astra, opus, grok), on an L step with `--tier top`, or when chalach names
  the model (e.g. `--model astra`).

## Lanes (who does what)
| Lane | For | Worker | Also good | Check |
|---|---|---|---|---|
| quick | answers, status | Grok Bot itself | sonnet, composer | none |
| lab-code | instrument code, sim notebooks | sol | astra, grok | opus |
| bug-fix | broken things | sol | astra | opus |
| numbers | fits, PSD lines, sensitivity | sol | opus, grok | opus |
| wiki | wiki pages, records, fab cards | sol | composer, gemini | sonnet |
| writing | text that leaves the lab | opus | astra, grok | sol |
| decision | design and physics choices | race | | none |
| loop | design loops | sol-fast | composer, sonnet | astra |
| repo | repos, refactors | sol | astra | opus |
| scout | papers, datasheets, the web | grok | sol | opus |
| mechanical | renames, links, tables | composer | gemini | none |

`ck-run route --lane <name>` prints the lane resolved to agent and model, and skips agents that are rate-limited.
The check runs only with a review reason (`leaves-lab`, `hardware-or-raw`, `agent-unsure`, `you-asked`).
Claude models edit only `~/PhD_local`; in a OneDrive workspace a non-Claude model does the edit.

## Changing a lane
`skills/reflect` proposes a change with numbers from `ck-run scoreboard --since 30d`. chalach approves it. Then the
line in `models.json` changes (ck-dispatch is protected: chalach's OK first), and this page follows.
