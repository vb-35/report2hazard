# TODO

## Interface

- [ ] **Mark interrupted localizations as failed on server start.**
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

- [ ] **Stop rejecting placements for an empty `reason_is_hypo`.** This is the most common validation error.
  Either fill it deterministically when the part is not a hypothesis, or flag it more clearly in the
  placement prompt/schema.
- [ ] **Cap result confidence to its parts instead of rejecting the call.** "result location/boundary_confidence
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
- [ ] **Record the reasoning effort and whether a call is a repair in `timings.jsonl`.** Today neither is logged;
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
- [ ] **Re-review only the corrected segments.** After 7 correction calls the review ran again for all 9
  segments (~15 min).
