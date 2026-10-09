# Multi-hazard report pipeline

The pipeline extracts a single qualitative report, detects its languages offline, translates when it is not mainly English, builds one ordered causal chain, categorizes every segment, evaluates the complete candidate, and pauses for a real human decision. Authoritative `final_rows.json` and `final_rows.csv` files are created only after approval.

Workflow: extraction → translation → segmentation → categorization → candidate report → self-evaluation/correction → human review → final export.

## Report grouping

One run contains only files explicitly selected together as companion parts or supplements of one logical report. The pipeline does not guess relationships from filenames. Put unrelated reports in separate input directories or upload them as separate runs. This rule and the selected files are recorded in `source.json` and `manifest.json`.

Supported inputs are DOCX, PDF, and UTF-8 TXT files containing English, German, French, or Italian text. `source.json` remains the authoritative extraction. `translated.json` preserves every original chunk and its provenance and adds `source_language` and `translated_text`; English chunks pass through without semantic rewriting. Segmentation interprets translations and original-language passthrough directly, produces English event/process fields, and keeps evidence quotes as exact or whitespace-normalized substrings of the original text. Unchanged causal steps retain their segment IDs across revisions; newly introduced steps receive new IDs independently of causal order.

Lingua 2.1.1 detects language spans locally with confidence and paragraph/page context checks; no detection request uses the LLM. The report gate counts alphabetic characters across all selected companion files, reassembling split source chunks first. More than 75% confidently English text skips translation entirely, retaining minority-language passages for segmentation. Exactly 75% enables translation. Unresolved text contributes to share bounds; when those bounds cross the threshold, processing fails explicitly without a translation call. Reports with no alphabetic text also fail. Detection is a heuristic restricted to four languages and does not guarantee recognition of unsupported languages or corrupted OCR.

`PipelineConfig.mainly_english_threshold`, `language_min_confidence` (0.80), and `language_min_margin` (0.20) are recorded per run and reused by corrections. Translation batches include only substantive non-English, mixed, or unresolved chunks, with glossary columns projected to English plus the batch's source languages. `translated.json.language_analysis` records detector version, counts, share bounds, spans, and translation flags; the manifest and review page show a compact summary. Zero-request translation stages are marked `skipped` while retaining the artifact. Old artifacts without analysis keep their English-translation contract until translation is explicitly rerun.

Verified on Windows with Python 3.13.9 and the Lingua 2.1.1 Windows wheel. Offline checks cover all four languages, mixed passages, and saved extracted report text. In a saved Schnannerbach extract, the English share bounds were 88.8%–98.6% and translation batches fell from one to zero; the German Vals report retained one batch. Local analysis took approximately 0.48 seconds including cold detector loading and 0.02 seconds for the subsequent Vals check; analysis reuse took about 0.002 seconds. These are local checks with fake translation responses, not live end-to-end runtime measurements. With standard ASCII JSON serialization, the glossary measured 18,858 characters before projection, 9,345 for German, and 13,290 for French plus Italian; these are character counts, not token counts.

## Easy interface (Windows)

Before processing reports, set `TW_LLM_API_BASE_URL` in your Windows user environment to your provider's API base URL (including its API version path). The app reads the saved Windows user setting if the launcher's inherited environment lacks it. Restart an already-running interface after changing the setting. No service address is bundled. Browsing existing results does not require this setting.

Requires Python 3.11 or newer. From the project folder, create an environment and install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Double-click **Start Interface.cmd** in the project folder. It opens the app in your browser using a free local port. If asked, paste your LLM API key (the input is hidden); it is used only for this session. Press Enter without a key to browse existing results. Keep the launcher window open while using the app.

New runs default to `Inferact/Qwen3.8-27B-NVFP4` with `medium` reasoning and a 660-second request timeout, matching the recent Qwen benchmarks. These defaults also apply to the command line. Restart an already-running interface to load them.

1. Choose a PDF, DOCX, or TXT report and click **Extract report**.
2. Wait for processing, then check the events and expand their source evidence.
3. Click **Approve report**, then **Download approved CSV** to use the results in Excel.

Previous reports remain under **Your reports**. Results are saved in the ignored `results` folder. To request changes, expand **Add an issue or request a correction** on the review page. On a new computer, install Python and run `python -m pip install -r requirements.txt` once before launching.

On Windows, the pipeline uses extended-length filesystem paths for deeply nested project folders, including PDF splitting and exports. No Windows long-path setting change is required.

## Command-line usage

Install dependencies and set the server-side LLM key:

```powershell
python -m pip install -r requirements.txt
$env:TW_LLM_API_KEY = "..."
$env:TW_LLM_API_BASE_URL = "<your provider's API base URL>"
```

Run through self-evaluation:

```powershell
python -m multi_hazard_pipeline run INPUT_DIR results
```

The legacy two-argument form remains valid. A passing candidate stops at `awaiting_human_review`; it is not yet a success or approved export.

Human operations use the same backend as the web interface:

```powershell
python -m multi_hazard_pipeline approve RUN_ARTIFACT_DIR --comment "Approved"
python -m multi_hazard_pipeline reject RUN_ARTIFACT_DIR --comment "Reason"
python -m multi_hazard_pipeline correct RUN_ARTIFACT_DIR --stage categorization --segments 2,4 --comment "Reclassify these segments"
python -m multi_hazard_pipeline correct RUN_ARTIFACT_DIR --stage translation --comment "Correct the terminology"
```

Exit codes are 0 approved, 1 failed, 2 invalid command usage, 3 awaiting human review, 4 revision required, and 5 rejected.

## Repeated model comparison

`benchmark_models.py` runs the real pipeline across explicitly selected reports and independent repetitions. It leaves the application's default model unchanged. List currently available IDs with `python benchmark_models.py models`. Prepare a JSON specification with `models` (explicit API IDs), `repeats` (at least 2), and `reports` (at least 2 objects containing a unique `id` and an `inputs` list of companion file paths). Optional `timeout_seconds` defaults to 660 to allow the router's 600-second backend deadline to return. Optional `model_reasoning_effort` maps explicit model IDs to their requested effort, for example `{"Inferact/Qwen3.8-27B-NVFP4": "medium"}`. The effort is recorded per run; models absent from that mapping retain their defaults. Reasoning benchmarks check the router's OpenAPI schema before launch to prevent silently ignored settings; `router_openapi_url` can point to that schema through an SSH tunnel when it is not publicly exposed. Other pipeline settings, prompts, batching, temperature, three-attempt call budget, and two-round correction budget are identical between models.

```powershell
python benchmark_models.py prepare results/run_support/model_benchmark_spec.json results/run_support/model_benchmark
python benchmark_models.py run results/run_support/model_benchmark
python benchmark_models.py summarize results/run_support/model_benchmark
python test_benchmark_models.py
```

The prepared plan hashes inputs, code, schemas, and glossary and records settings and dependency versions. All reports and repetitions run with the first model before switching to the next model, minimizing loading overhead. This fixed order can confound timings with changes in server load; repeat a later benchmark in reversed model order if that matters. A small availability warmup before each model switch records loading time and calls separately. Warmup time is excluded from report timings; the first full report can still incur prompt/schema cache warmup. The benchmark runs one report at a time; use an otherwise idle API for a controlled timing comparison. The suite directory must be new. Completed jobs can be skipped on rerun; an interrupted running job stops for inspection instead of overwriting artifacts.

`comparison.md`, `summary.json`, and `runs.csv` update after each run. They include wall time and its variation, stages, reviewer readiness, candidate availability, first-attempt call pass rate, extra attempts, request/parsing/validation failures, retry wait, semantic correction rounds, and actual token usage with reporting coverage. Failure-adjusted time includes the time spent on failed runs. Unknown-label fraction and segment count are diagnostics, not quality scores. An exact-source quote and chain/label validation checks structure; they do not establish that a statement or causal link is justified.

For semantic quality, score the anonymized `review/C*.json` packets against their complete original source before opening `model_key.json` or the model timing table. Fill `ratings.csv` with 0–4 ratings for factual support (claims match source and uncertainty), taxonomy (T1–T5 labels), completeness (important source-supported steps captured without duplicates), causality (only justified direct links), and translation (preserved meaning, terminology, numbers and uncertainty). Score each dimension: 4 = no material error, 3 = minor errors, 2 = one material error or several minor errors, 1 = multiple material errors, 0 = unusable. Check the *whole* source to assess omissions. Record erroneous labels, unsupported links, and missing steps in `notes`; refer to segment IDs and source quotes. For translation skipped by the language gate, assess whether original-language interpretation is preserved. Missing candidates receive 0 for all dimensions with the failure recorded. Unfilled ratings remain pending rather than becoming zero. Re-run `summarize` after scoring for totals out of 20. For stronger ground truth, adjudicate reference steps, labels and links from the sources before reading any candidates; repeat scoring with a second reviewer on a subset.

Compare both models within each report before pooling. Prefer the model with fewer source-grounded material errors and missing steps, then compare its first-attempt reliability and failure-adjusted time. Reviewer pass rate alone does not determine the winner. Three repetitions on four reports are a pilot from one report collection, not a statistically conclusive comparison across languages or report domains. Extend the specification with independent reports and more repetitions if the results are close.

## Local web review

```powershell
python -m multi_hazard_pipeline serve results --host 127.0.0.1 --port 5000
```

Open `http://127.0.0.1:5000`. The local server accepts uploads, runs one job at a time in a background thread, polls artifact manifests, and supports review, edits, correction requests, rejection, approval, and downloads. The LLM key never enters browser HTML or JavaScript.

Each run opens a workspace with a searchable segment table, selected-segment evidence and editing, an expandable report reader, and persistent review actions. Original, Translation, and Compare modes follow chunk IDs across companion documents; citations highlight normalized quotations in the original extracted text and the corresponding translated chunk. Repeated or unmatched quotations use an explained containing-chunk fallback. Original PDF mode uses the browser's PDF viewer and cited page references; DOCX and TXT remain available as extracted text and source downloads. Polling preserves selection, filters, reading position, and unsaved input, sends report text only when its artifacts change, and labels retained results during corrections. Older runs without revision identities wait for stage completion before treating downstream artifacts as current.

Single-PDF preparation also runs on that worker. Uploads open a progress page immediately; when a collection is separated, that page links to each event's independent run. Preparation failures remain visible in the parent manifest.

This version intentionally uses a single-process, in-memory worker. Restarting the server loses queued/running jobs, while completed artifact directories remain reopenable. Do not enable Flask's development reloader because it can duplicate the worker. The UI has no authentication or CSRF layer and is intended only for localhost; do not bind it to an untrusted network.

The correction maximum is run-wide, including automatic and human-requested reruns. Corrections receive structured review issues with affected segment IDs, the previous chain, translations, labels, and rationale. All feedback survives downstream reruns, including simultaneous segmentation and categorization issues and unresolved issues accompanying a human request. Correction requests use permanent citation IDs throughout their context; `review_chunk_references` maps the evaluator's request-local IDs back to the source.

A translation correction reuses matching saved language analysis and repairs the chunks reliably located through affected segments' source citations, retaining other translations. For mainly English reports, translation remains skipped and feedback reaches segmentation and categorization. Scoped segmentation corrections revise the existing complete chain and its dependencies, permitting additions, removals, merges, reordering, and changes to other affected steps. Unchanged steps retain stable IDs and source provenance. Broad or unlocatable scopes use a full rerun; a repair request exceeding the request-size ceiling falls back to normal batched segmentation. Correction batches are not automatically halved.

Categorization receives the whole chain and may correct additional affected segments. Existing classifications are retained only for unchanged content and causal context; changed steps, their causal neighbors, and other steps in the same event are reassessed conservatively. Every corrected candidate receives whole-report evaluation against the original source and translated/passthrough text before human approval. The evaluator checks translation fidelity where translation was applied and treats missed minority-language content as a segmentation issue.

Candidates in `revision_required` remain editable. Manual edits can be re-evaluated even when automatic correction rounds are exhausted; approval requires a passing evaluation. Invalid or unchanged edits and invalid correction requests display an error without modifying saved artifacts.

All three classification fields allow `unknown` when the field applies but the evidence is insufficient, and `not applicable` when the field does not apply to the evidenced step. The classification rationale must explain the choice. Pre-event conditions or triggers without sediment movement or retention/blockage use `not applicable` for sediment transport phase rather than defaulting to `Transportation`.

Classification and independent review now insert the same `TAXONOMY_DECISION_RULES` from `multi_hazard_pipeline/config.py`: T1 evidence/applicability, T2 infrastructure connectivity direction, T3 causal role, T4 interaction precedence, and T5 sediment phase. This replaces their separate disambiguation blocks and removes the classifier's fixed-label Schnannerbach calibrations. T2 assigns `Positive Impact on permanent or temporary infrastructure` to evidenced increased sediment passage, `Negative Impact on permanent or temporary infrastructure` to evidenced decreased passage, and unqualified `Impact on permanent or temporary infrastructure` to an affected/involved structure with unclear direction. Damage, protective purpose, and traffic closure alone do not establish that direction or sediment dysconnectivity.

The reviewer must independently assess source evidence, then put the affected segment, field/current label, source chunk ID/short quote, and violated rule ID/explanation in each categorization disagreement's existing `message`. Its concise `suggested_action` proposes a replacement only when supported. The classifier treats correction instructions as claims to reassess, can retain a supported label, and explains unsupported replacement proposals. The schema, controlled vocabulary, correction limit, and model stages are unchanged.

Segmentation and consolidation now order evidenced causes before effects while allowing independent branches and later steps with empty predecessors. Chronology, adjacency, and shared location alone do not establish a predecessor link. All four unsupported links were removed from the Schnannerbach example; its backflow-to-flooding claim remains within one step. A second example explicitly uses `[]`, `[]`, `[2]`, `[]`: a later unrelated tributary event does not follow causally from road damage, while the stated tributary sediment-to-basin relationship does.

DOCX extraction includes tables and nested tables in document order, with table, row, and cell provenance. Long source chunks are divided before translation, retaining their parent ID and zero-based character offsets (exclusive end). Model payloads project only stage-relevant fields, retain document boundaries and source locations, and omit `translated_text` when identical to `text`; prompts then require direct interpretation of the original language. Batches count the original and any distinct translation actually sent; oversized chunks fail explicitly. Requests use compact Unicode JSON and request-local citation IDs, resolved to permanent IDs before evidence validation and saving; unknown references are rejected. Saved artifacts retain full source text, translations, provenance, and permanent IDs. Classification supplies controlled labels in its prompt and schema without repeating the list in its input; review retains its input taxonomy because its response schema contains no classification labels.

`LLMConfig.max_request_chars` caps the complete serialized request, including its schema, at 200,000 characters by default. Timing logs count the serialized model input actually sent. These are character measurements, not token or runtime savings: whole-report evaluation above the ceiling fails before submission, so adjust the ceiling to your provider's capacity when needed.

`LLMConfig.retries` is the maximum total attempts per model call, including request retries and output repairs. Oversized requests, invalid local configuration, authentication/unsupported-request HTTP errors, and explicit exhausted billing quota fail immediately. Transient connection errors, timeouts, HTTP 408/409/425/429, and server errors (except 501/505) retry with bounded exponential backoff, honoring `Retry-After` seconds/dates or `retry-after-ms` when supplied. Parsing and validation repairs receive the failed answer and specific error while retaining the original instructions, evidence, and response schema. Only the faulty-answer copy may be truncated to fit; if the preserved context and feedback cannot fit, repair fails locally. Partial, failed, refused, and malformed/trailing JSON output cannot pass through as completed answers.

Citation verification has a 45-second limit per attempt and a 90-second total budget per citation, including retries, output repairs, and retry delays. Each attempt is also capped by the configured request timeout and remaining budget. A retry whose delay cannot fit fails immediately. Exhaustion fails segmentation without accepting unverified evidence or restarting the parent model call. The time budget applies separately to each citation, not to the report as a whole. Timed-out requests may still finish on the server; their late responses are discarded.

Initial runs, human correction requests, and reviews after manual edits append model calls to the run's `timings.jsonl`. Each attempt distinguishes request, parsing, and validation failures, records its effective timeout, total time budget and retry delays, and preserves the endpoint's actual `usage` object when supplied; token counts are never estimated from characters.

## Run states and artifacts

Run folders use the input report name followed by the UTC start time, for example `schnannerbach__2026-09-28_08-36-16-123456Z`. The final six digits are microseconds to distinguish closely spaced runs. Companion-file runs use the first selected file's name; split collections use each event report's filename.

Statuses are `running`, `revision_required`, `awaiting_human_review`, `rejected`, `failed`, and `approved`. A collection parent uses `split` once its child runs have been created.

Each extraction run directory preserves `source.json`, `translated.json`, `segments.json`, `classified.json`, `candidate_report.json`, `self_evaluation.json`, `human_review.json`, and `manifest.json` as stages complete. Collection parents hold their preparation manifest and child-run links. Only approved runs add authoritative `final_rows.json` and `final_rows.csv`.

Run the small offline research smoke check without an API key. It exercises the real pipeline with fixed model responses, covering extraction/provenance, translated and untranslated payloads, batching, request-local citation identity and unknown references, Unicode serialization, translation/glossary, evidence, classification coverage, human edits and approval, and CSV/JSON export. Network access is blocked:

```powershell
python test_regressions.py
```

The check also verifies shared-rule prompt wiring, the corrected example's literal citations, valid branches/multiple roots, controlled labels for three fixed infrastructure fixtures, and targeted correction/review routing without automatic adoption of a reviewer proposal. It deliberately shows that an unsupported link to an earlier ID still passes structural validation. These are structural and integration checks with mocked responses: they cannot prove improved classification, causal inference, or reviewer behavior.

Run the focused offline correction check as well:

```powershell
python test_corrections.py
```

Run `python test_llm.py` for focused offline client checks: permanent/transient failures, server retry guidance, attempt limits, faulty-answer repair and request ceilings, partial-output rejection, actual usage, human-operation timing, and failed-save/approval gates. Network access is blocked.

It verifies a correction to one segment changes another segment's dependency and classification while retaining independent work, followed by complete report review. It also covers simultaneous feedback, retained translation chunks, chain additions/removals/merges/reordering, broad and request-size fallbacks, correction limits, and approval gating. Responses are fixed and network access is blocked; this checks workflow behavior, not model semantic quality.

Proposed semantic comparison (not run; live calls require authorization): use these three saved runs' original `source.json`, frozen `translated.json`, `segments.json`, candidate, and review history.

| Saved run under `results/` | Evidence questions to adjudicate |
| --- | --- |
| `01-bergsturz-vals-pages-036-042__2026-10-06_09-22-44-564311Z` | Segment 4: blocks partially destroyed protection nets; no direction is stated. Earlier reviewer rounds proposed Negative solely from damage, and the final classifier adopted that rationale. T2 supports unqualified Impact unless additional cited evidence establishes direction; rejecting Positive does not establish Negative. Segment 5: traffic isolation does not establish sediment blockage. |
| `11-erlachgraben-pages-154-158__2026-09-28_16-15-50-805105Z` | Segment 6: barrier clogged with wood and sedimentation area filled (retention). Segment 7: examine whether the masoned channel conveys sediment or only water; protection from damage alone cannot justify Positive. |
| `09-hassbach-gemeinde-warth-pages-136-146__2026-09-29_06-41-24-385931Z` | Segments 7/9/11: distinguish backwater, jamming, and basin filling/downstream erosion using the cited mechanism. Segment 12: undermined bridge foundation does not by itself prove decreased connectivity. Check the saved reviewer objections to predecessor links against source causality. |

First manually adjudicate labels and direct causal links from source passages, retaining uncertainty. Compare old versus revised classification/review on the same frozen segments to isolate taxonomy effects; separately compare segmentation on the same frozen source/translation to count unsupported links and missed supported links. Keep model, temperature, batching, and correction budget identical. Record source-supported label accuracy (including unclear direction), evidence-plus-rule coverage of disagreements, unsupported replacement proposals, correction label flips, omitted steps, and output length. Reviewer pass rate alone is not a quality metric. Preserve saved artifacts and write any authorized comparison into a separate run directory.

OCR, geospatial/GIS support, and external knowledge retrieval are deliberately deferred. Scanned-PDF OCR is not supported, and Tesseract `.traineddata` files are not required.

## Repository contents and local data

The repository includes the application, one research smoke check, and the required `Translation resources/multi_hazard_keywords.csv` glossary. Keep that CSV at its existing path. Keep future checks focused on research results and data integrity; detailed UI, launcher, and exhaustive edge-case suites are unnecessary for this project.

All local outputs belong in `results/`, which is excluded by `.gitignore`. The launcher and CLI default to this folder; PDF splitting defaults to `results/split/`. Explicit CLI output directories remain supported. Preserved source documents live in `results/inputs/`, spreadsheet exports in `results/exports/`, and supporting logs and split inputs in `results/run_support/`. Run directories are directly inside `results/` so the review interface can find them. Obsolete local files are collected in `results/obsolete/`. Python environments and caches are also ignored.

The smoke check uses synthetic inputs and temporary outputs. `check_example.py` is an optional local integration check requiring `results/inputs/tmp_single_input/Schnannerbach_extract1.docx` and a live API key; it replaces `results/example_check` when run.

Processing requires `TW_LLM_API_BASE_URL` and `TW_LLM_API_KEY` in the environment before starting the application. Use a compatible chat-completions service; the client appends `/chat/completions` to the base URL. Configure the model in `multi_hazard_pipeline/config.py`. 
