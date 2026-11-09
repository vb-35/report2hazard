# TODO

## Interface

- [x] **Mark interrupted localizations as failed on server start.** Done 2026-11-06: queued/running work records
  its process (`worker` = pid and host, for extraction runs and localizations); `create_app` marks work as failed
  only when that process no longer exists on this machine. Runs of other live processes (command line, night batch,
  a second interface) and runs without a marker are left alone. Original note:
  If the interface server stops while a localization is running (for example, the console window is closed), the
  run's `manifest.json` keeps `localization.status = "running"`. The run page then shows "running" forever,
  although no process is working on it and no model calls are made.
  Seen on 2026-11-03 with `results/seigesbach__2026-11-03_10-10-27-872742Z`, which stopped at
  "model call 4: segment_1".
  - Fix: in `create_app` (`multi_hazard_pipeline/web.py`), scan the runs under `ARTIFACT_ROOT` once at
    startup. Set every localization still `queued`/`running` to `failed`, with an error such as
    "Interrupted: the interface stopped before the localization finished" and a `finished_at` time. For
    localization-test runs, also call `provided.finish_stage(run_dir, "failed")`.
  - Keep the extraction approval: as in the existing failure path, only the localization stage is marked failed.
  - Check whether extraction/review runs (`web.py` `work()`) can get stuck in the same way, and handle
    them with the same startup step.
  - Test: write a run whose manifest says `running`, call `create_app`, and check that the status
    endpoint reports `failed` with the interruption message.

## Localization speed

Measured on `results/seigesbach__2026-11-03_10-36-13-502354Z` (9 segments, Qwen3.8-27B, reasoning effort
`medium` for every call, 24,000-token output cap): 65 model calls, about 157 min of generation, about 2 h 55 min
from start to finished map. Placement used 101 min, but only 16 of its 35 calls were accepted.

- [x] **Stop rejecting placements for an empty `reason_is_hypo`.** This is the most common validation error
  (43 rejections, ~145 min over the five Austrian reports on 2026-11-04). Done 2026-11-06: the placement prompt
  defines `reason_is_hypo` and `reason_confidence` (a sentence, not a level word), and the schema requires the part
  text fields to be non-empty (`minLength: 1`). Check after the next run that the server enforces `minLength`.
- [x] **Cap result confidence to its parts instead of rejecting the call.** Done 2026-11-06 for the localization
  agent (`compile_part(..., adjust=True)`): over-high part/result confidences, boundary confidence on parts without an
  event boundary, unresolved results with a confidence, and visual observations without `evidence_type=mixed` are now
  lowered or set by code and recorded as "Code adjustment: ..." in `reason_confidence`/`reason`. The stored answer
  is synced with the built part (rule 5d values included), so the review no longer sees two different confidences.
  The `placements.json` toolkit still raises. Original note: "result location/boundary_confidence
  cannot exceed the confidence of its supporting parts" can be fixed deterministically by lowering the result
  value; record the adjustment in the output. Check whether "high requires a uniquely named GIS feature" and
  "assumed extent cannot have medium/high boundary confidence" can be lowered the same way.
  Together with the item above, these two account for most of the ~55 min spent on rejected placement calls.
- [ ] **Try lower reasoning effort per task**, measured with `benchmark_models.py` on the existing events
  before changing defaults. Today one `reasoning_effort` (`LLMConfig`, `medium`) is used for every task;
  only the recovery retry after a truncated answer drops to `low`.
  - Candidates: review (26/26 calls accepted, 88% of its output is reasoning), frame and evidence linking
    (77% reasoning).
  - Keep `medium` for placement until tested: its rejections are rule violations, and less reasoning may
    make them more frequent.
  - Needs a per-task effort setting in the localization agent (and its checkpoint fingerprint).
- [ ] **Keep the 24,000-token cap; handle runaway reasoning instead.** The cap is working: accepted placements
  use at most 13,110 tokens (median ~4,000), while 2 of the 3 truncated placement calls spent all 24,000
  tokens on reasoning without starting an answer. Consider retrying those sooner at `low` effort rather
  than waiting ~7 min for each to hit the cap.
- [x] **Record the reasoning effort and whether a call is a repair in `timings.jsonl`.** Done 2026-11-06: each row
  has `model`, `reasoning_effort`, `max_output_tokens` and `repair_of` (null, or the failure kind it repairs). Today neither is logged;
  on 2026-11-03 it had to be reconstructed from the absence of `*_recovery.json` files (the low-effort
  recovery never ran: all 35 placement calls were `medium`). Analysis of that run: first attempts use
  3,500–24,000 reasoning tokens and are usually rejected; repairs (failed answer + error sent back, same
  effort) use 200–1,600 and usually pass. 12 of the 16 accepted placements were repairs, which explains
  the low median (~4,000 output tokens). Use this split when benchmarking lower effort for placement.
- [ ] **Save failed and cut-off responses, and detect whitespace loops.** The client keeps only the final answer
  (`_response_text`, `llm.py`) and never writes rejected answers or reasoning to disk, so runaway calls can't be
  inspected. Write the raw response of every `parsing_error`/incomplete call (head and tail, size-limited)
  next to `timings.jsonl`. On 2026-11-03, 1 of 4 cut-off calls (placement, 10:58:58) produced 22,456
  answer tokens in only ~35k characters (1.5 chars/token vs 3.8–9.2 for accepted answers): very likely
  the old blank-space loop, now inside the JSON answer (whitespace is valid JSON). The other 3 reasoned at
  normal text density until the cap. Consider stopping a streamed answer early when it is mostly
  whitespace, instead of waiting ~7 min for the cap.
  - Replay test 2026-11-03 (`tmp/replay/`): the first-attempt requests of segments 1, 3 and 9 (the three
    that hit the cap) were resent unchanged, one attempt each, with raw responses saved. 6 completed: none hit
    the cap, no whitespace loop (longest whitespace run 11 chars). The runaways did not reproduce, so they are
    intermittent. One response (segment 9) showed the "keeps reasoning" mode: it says "Now, let me write the
    final JSON." 15 times, first at 60% of its 15,715 reasoning tokens, each time followed by "Actually, I
    need to reconsider…" on the same question (the 7.5 ha fan area). The server does return the reasoning
    text, so saving it is possible.
- [ ] **Keep finished reviews in the checkpoint.** After the resume on 2026-11-03, reviews 1–8 were redone
  (~9 min).
- [x] **Re-review only the corrected segments.** Done 2026-11-06 (vision mode; text-only mode still reviews in one
  call). After 7 correction calls the review ran again for all 9
  segments (~15 min).

## Localization quality (night batch 2026-11-04, five Austrian reports)

Runs in `tatzelwurm:~/report2hazard/results/*__2026-11-04_13-3*` (batch files in `~/report2hazard-batch-20261104/`).
The Swiss Glyssibach, Rotlauibach and Trachtbach reports hold almost no location information and are excluded from
evaluation and future runs. 13 of the 24 open review issues were review artifacts (fixed 2026-11-06: rule 5d values
synced into the answer; review fields no longer cut at 500 characters).

- [ ] **Figure captions fail quote verification in evidence linking.** Firschnitzbach 33 (deposition behind the
  arch dam) stayed unresolved: its three "Abbildung 67-69" captions naming the Bogensperre were dropped as "quote not
  verified", and table 15 with the dam stations (hm 18.18/18.38) was never linked. Check `resolve_quote` against
  caption text (spacing, hyphenation, captions only in `report_captions.json`).
- [ ] **Fit reported areas automatically.** The model sets `report_area_ha` but not `fit_report_area`, so the
  assumed band height gives areas far from the report: Schnannerbach 26 (11.9 ha mapped / 2.73 ha reported),
  Schallerbach 15 (14.75 / 8 ha), Seigesbach (2.5 / 7.5 ha). Consider fitting when the report area and the reach
  limits both come from the report.
- [ ] **Run-to-run variance.** Seigesbach segment 3 was located on 2026-11-03 and unresolved on 2026-11-04.
  Repeat runs before comparing models or prompts (`benchmark_models.py` requires at least 2 repeats).
- [x] **Client timeout raised from 660 s to 1200 s** (2026-11-06), now that the GPU router no longer stops requests at
  600 s; under three parallel reports the slowest placement call took 575 s.
- [x] **Placement parts require at least one quote** in the answer schema (12 rejections, ~50 min on 2026-11-04),
  except when verified report outlines are offered.

### Rerun 2026-11-06 (same five reports, all fixes above)

Runs in `tatzelwurm:~/report2hazard/results/*__2026-11-06_12-0*` (batch files in `~/report2hazard-batch-20261106/`).
1 h 37 min instead of about 2 h 40 min; 156 calls (was 242), 12 rejected (was 62), 7 open review issues (was 24),
46 segments located and 4 unresolved (was 45/5). Schnannerbach and Firschnitzbach were delivered.

- [x] **Accept close area fits** (2026-11-06). Schallerbach 15a: the 8 ha fit reached 9.4 ha (17.5% off, above the 5%
  tolerance); the repair hint then made the model drop the report area for an assumed height, giving 17.6 ha and a
  rule-5d hypothesis. Fits up to 25% off are now accepted with boundary_confidence low; the hint asks to keep the area.
- [x] **Rule 5d no longer contradicts the model's is_hypo reason** (2026-11-06). When code raises is_hypo from 0 to 1,
  the reason is replaced and the model's sentence kept only as a "Code adjustment" note.
- [x] **A misquoted station anchor no longer fails the report** (2026-11-09). Seigesbach (rerun 2026-11-06 16:00)
  failed in the frame call: the model wrote "linksufriger Zuflinger bei hm 18,3" three times for the report's
  "Zubrin- ger". Quote errors now show the closest report wording, and an anchor whose quote still fails on the
  last attempt is dropped with a note instead of failing the run.
- [ ] **Source discrepancies keep a run in revision.** Seigesbach figure 71's legend says "Ereignis vom 7. und 8. Juni
  2016"; the report says 7.6.2015. The reviewer flags it on every part citing the figure, but placement cannot fix it.
  Consider recording such report inconsistencies as notes rather than open issues.
- [ ] **Real placement misses flagged by review:** Schallerbach 17 (Grieß, Elis and B 188 deposition not mapped),
  Maierhofgraben 41 (reach ends at hm 24, report says at least hm 28), Seigesbach 6a (contour heights 1500–2500 m read
  as distances from the mouth).
