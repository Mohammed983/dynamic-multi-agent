# AQUA-RAT v2: independent-agent diagnostic — midterm results

**Preliminary, incomplete run; offline analysis only.** Gemini 3.5 Flash-Lite, minimal thinking; four distinct reasoning roles, one independent round, zero edges, 2,048-token output cap. All primary accuracies below use the **16 completed questions**, not 20 planned questions. Correctness means agreement with the frozen benchmark option label.

## Coverage and accuracy

| Coverage | Count |
|---|---:|
| Complete questions | 16/20 (80%) |
| Partial question | 1/20 |
| Not attempted | 3/20 |
| Generation attempts | 66/80 |
| Complete responses / truncated responses | 65 / 1 |

| Agent / strategy | Correct / completed | Accuracy |
|---|---:|---:|
| 1 — Step-by-step solver | 14/16 | 87.50% |
| 2 — Alternative solver | 14/16 | 87.50% |
| 3 — Critical reasoner | 15/16 | 93.75% |
| 4 — Independent verifier | 14/16 | 87.50% |
| **System plurality vote** | **14/16** | **87.50%** |

The project calls this majority voting, but it selects the most frequent parsed option (plurality); ties use alphabetical order A < B < C < D < E. Invalid choices abstain; no reference is consulted. All 64 responses in completed questions parsed successfully. There were two 2–2 ties: validation:126, A vs D → A (label match), and validation:68, C vs D → C (label mismatch). One tie was correct and one incorrect; tie behavior affects the score.

The partial item and three unattempted items are excluded from completed-question accuracy. The planned-denominator majority count is 14/20 (70%) with 4 missing outcomes; this is a coverage-sensitive count, not accuracy on 20 answered questions. No missing response is imputed.

## Agreement and error diversity

| Metric on 16 complete questions | Count | Percentage |
|---|---:|---:|
| Disagreement (at least two distinct choices) | 3/16 | 18.75% |
| At least one label-correct and one label-incorrect agent | 3/16 | 18.75% |
| Unanimous, label-correct | 13/16 | 81.25% |
| Unanimous, label-incorrect | 0/16 | 0.00% |

| Item | Agents 1 / 2 / 3 / 4 | Reference | System vote | Result |
|---|---|---|---|---|
| validation:126 | D / A / A / D | A | A | Correct |
| validation:71 | A / B / B / D | A | B | Incorrect |
| validation:68 | C / C / D / D | D | C | Incorrect |

These three items expose differing final choices, not demonstrated communication benefits. At validation:126, Agents 1 and 4 calculate 24 days but select D (28), suggesting answer-to-option mapping inconsistencies rather than solely failed arithmetic. At validation:71 the correct choice exists in one agent but the plurality selects B. The strongest individual agent scored 15/16, exceeding the system vote in this small sample. No peers exchanged messages.

## Exact interruption and data-quality caveat

At validation:146, Agent 1/node 0 returned STOP and chose B (label A; excluded from the 16-item analysis). Agent 2/node 1 returned **MAX_TOKENS**, not STOP, before a final answer. Error: `Gemini response was blocked, truncated or incomplete; recorded usage is retained. finish_reason=MAX_TOKENS; candidates=1.`

The failed call ID is `1ec620a1-8f61-4589-91b5-1092b5a76d57`. It reports 266 input, 2,044 output, zero thinking tokens and 5.930364 seconds. HTTP status is null; no quota, authentication, model-access or timeout failure is recorded. The backend rejects truncation before answer parsing, and the diagnostic halts on every incomplete response. Nodes 2/3 of this item were not called; validation:66, :215 and :14 were not attempted. The 150-word prompt instruction is soft; it did not prevent lengthy reconsideration and output-cap exhaustion.

**Data-quality flags (not rescored or excluded):** validation:146 gives `0.05×3 + 0.20x = 0.10(3+x)`, hence x = 1.5 quarts. No displayed choice contains 1.5; the reference A is 2 quarts. Both returned texts derive 1.5 and then reconsider the options. This mismatch plausibly contributed to verbosity, but is not a measured causal explanation. For validation:68, the stated equations yield cost 12.5 and original sale price 13.75, also absent from its options (reference D is 13.5). These are local arithmetic consistency flags requiring dataset review; official-label metrics above remain unchanged.

## Actual tokens and latency

| Scope | Attempts | Input tokens | Output tokens | Total tokens | API request latency (s) |
|---|---:|---:|---:|---:|---:|
| 16 complete questions | 64 | 18,284 | 29,449 | 47,733 | 113.908 |
| Partial validation:146 | 2 | 518 | 3,872 | 4,390 | 11.063 |
| All recorded attempts | 66 | 18,802 | 33,321 | 52,123 | 124.970 |

All 66 attempts have provider usage and measured latency; no unknown usage. Thinking tokens: 0. Mean summed API latency per complete question: 7.119 s. Mean complete-question wall time: 20.302 s; summed attempted-question wall time: 338.145 s.

API latency is client-observed request time, not isolated server compute. Wall time also includes pacing and logging. Partial-call usage is included in totals; cached input is not double-counted. These totals exclude the original v1 diagnostic, GSM8K and metadata reads.

## Comparison with the earlier GSM8K pilot

| Setting / result | AQUA-RAT v2 independent | GSM8K fully connected | GSM8K ring |
|---|---|---|---|
| Questions | 16 complete / 20 frozen validation | 10 validation | Same 10 validation |
| Format | Five-choice A–E | Numerical exact match | Numerical exact match |
| Model / thinking | Gemini 3.5 Flash-Lite / minimal | Same | Same |
| Agents / rounds | 4 roles / 1 independent | 4 generic agents / 3 | 4 generic agents / 3 |
| Output cap per call | 2,048 | 512 | 512 |
| Active edges by round | 0 | 0 → 12 → 12 | 0 → 4 → 4 |
| Initial plurality accuracy | 14/16 (87.5%) | 10/10 (100%) | 10/10 (100%) |
| Final system accuracy | Same as initial (no later rounds) | 10/10 (100%) | 10/10 (100%) |
| Initial disagreement | 3/16 (18.75%) | 0/10 | 0/10 |
| Known input tokens | 18,802 (all 66 attempts) | 75,200 (successful calls) | 44,002 (successful calls) |
| Known output tokens | 33,321 (all 66 attempts) | 22,498 (successful calls) | 22,175 (successful calls) |
| Successful-call API latency (s) | 119.040 (65 responses) | 179.118 | 214.984 |

GSM8K had 240 successful responses and one additional HTTP 504 attempt: 241 attempts overall. The full-graph timeout took 59.378 s with unknown token usage; its true all-attempt token total is unavailable. AQUA totals include its known truncated call; GSM8K token columns include successful calls only. These consumption/latency figures are not matched-budget comparisons.

Both GSM8K initial and final votes were already correct, with zero numeric answer changes across 160 agent transitions. AQUA shows more observed disagreement and lower label accuracy in its completed subset, but dataset, roles, prompts, output cap, round count, sample size and completion differ. Their individual effects cannot be isolated. Multiple-choice labeling and suspected option errors further limit interpretation. This small incomplete diagnostic does not prove communication superiority, causal role benefits, or successful error correction.

## Reproduce without API access

```powershell
.\.venv\Scripts\python.exe -m experiments.analyze_diagnostic_v2 --output-prefix results/aqua_v2_midterm_repeat
```

The script imports no inference SDK, recomputes choices/votes from raw returned text, checks stored diagnostics, reconciles all request starts/terminal events and rejects duplicate attempts. It writes new Markdown/JSON only, refuses existing output files, and verifies all pre-existing results remain unchanged. JSON contains per-question outcomes, source SHA-256 hashes and analysis-script hash. No training or experiment is started.

