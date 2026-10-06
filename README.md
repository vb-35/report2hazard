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

1. Choose a PDF, DOCX, or TXT report and click **Extract report**.
2. Wait for processing, then check the events and expand their source evidence.
3. Click **Approve report**, then **Download approved CSV** to use the results in Excel.

Previous reports remain under **Your reports**. Results are saved in the ignored `results` folder. To request changes, expand **Add an issue or request a correction** on the review page. On a new computer, install Python and run `python -m pip install -r requirements.txt` once before launching.

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

## Local web review

```powershell
python -m multi_hazard_pipeline serve results --host 127.0.0.1 --port 5000
```

Open `http://127.0.0.1:5000`. The local server accepts uploads, runs one job at a time in a background thread, polls artifact manifests, and supports review, edits, correction requests, rejection, approval, and downloads. The LLM key never enters browser HTML or JavaScript.

Each run opens a workspace with a searchable segment table, selected-segment evidence and editing, an expandable report reader, and persistent review actions. Original, Translation, and Compare modes follow chunk IDs across companion documents; citations highlight normalized quotations in the original extracted text and the corresponding translated chunk. Repeated or unmatched quotations use an explained containing-chunk fallback. Original PDF mode uses the browser's PDF viewer and cited page references; DOCX and TXT remain available as extracted text and source downloads. Polling preserves selection, filters, reading position, and unsaved input, sends report text only when its artifacts change, and labels retained results during corrections. Older runs without revision identities wait for stage completion before treating downstream artifacts as current.

Single-PDF preparation also runs on that worker. Uploads open a progress page immediately; when a collection is separated, that page links to each event's independent run. Preparation failures remain visible in the parent manifest.

This version intentionally uses a single-process, in-memory worker. Restarting the server loses queued/running jobs, while completed artifact directories remain reopenable. Do not enable Flask's development reloader because it can duplicate the worker. The UI has no authentication or CSRF layer and is intended only for localhost; do not bind it to an untrusted network.

The correction maximum is run-wide, including automatic and human-requested reruns. A translation correction reuses matching saved analysis and follows the same report gate. For mainly English reports, translation remains skipped and the correction instruction reaches segmentation and categorization. A segmentation correction also reruns categorization. Whole-report evaluation receives original text and translated/passthrough text, checks translation fidelity where translation was applied, and treats missed minority-language content as a segmentation issue.

Candidates in `revision_required` remain editable. Manual edits can be re-evaluated even when automatic correction rounds are exhausted; approval requires a passing evaluation. Invalid or unchanged edits and invalid correction requests display an error without modifying saved artifacts.

All three classification fields allow `unknown` when the field applies but the evidence is insufficient, and `not applicable` when the field does not apply to the evidenced step. The classification rationale must explain the choice. Pre-event conditions or triggers without sediment movement or retention/blockage use `not applicable` for sediment transport phase rather than defaulting to `Transportation`.

DOCX extraction includes tables and nested tables in document order, with table, row, and cell provenance. Long source chunks are divided before translation, retaining their parent ID and zero-based character offsets (exclusive end). Batches count both original and translated text; oversized translated chunks fail explicitly. `LLMConfig.max_request_chars` caps the complete serialized request, including its schema, at 200,000 characters by default. This is a character safeguard, not a token measurement: whole-report evaluation above the ceiling fails before submission, so adjust the ceiling to your provider's capacity when needed.

## Run states and artifacts

Run folders use the input report name followed by the UTC start time, for example `schnannerbach__2026-09-28_08-36-16-123456Z`. The final six digits are microseconds to distinguish closely spaced runs. Companion-file runs use the first selected file's name; split collections use each event report's filename.

Statuses are `running`, `revision_required`, `awaiting_human_review`, `rejected`, `failed`, and `approved`. A collection parent uses `split` once its child runs have been created.

Each extraction run directory preserves `source.json`, `translated.json`, `segments.json`, `classified.json`, `candidate_report.json`, `self_evaluation.json`, `human_review.json`, and `manifest.json` as stages complete. Collection parents hold their preparation manifest and child-run links. Only approved runs add authoritative `final_rows.json` and `final_rows.csv`.

Run the small offline research smoke check without an API key. It exercises the real pipeline with fixed model responses, covering extraction/provenance, translation/glossary, evidence, classification coverage, human edits and approval, and CSV/JSON export. Network access is blocked:

```powershell
python test_regressions.py
```

OCR, geospatial/GIS support, and external knowledge retrieval are deliberately deferred. Scanned-PDF OCR is not supported, and Tesseract `.traineddata` files are not required.

## Repository contents and local data

The repository includes the application, one research smoke check, and the required `Translation resources/multi_hazard_keywords.csv` glossary. Keep that CSV at its existing path. Keep future checks focused on research results and data integrity; detailed UI, launcher, and exhaustive edge-case suites are unnecessary for this project.

All local outputs belong in `results/`, which is excluded by `.gitignore`. The launcher and CLI default to this folder; PDF splitting defaults to `results/split/`. Explicit CLI output directories remain supported. Preserved source documents live in `results/inputs/`, spreadsheet exports in `results/exports/`, and supporting logs and split inputs in `results/run_support/`. Run directories are directly inside `results/` so the review interface can find them. Obsolete local files are collected in `results/obsolete/`. Python environments and caches are also ignored.

The smoke check uses synthetic inputs and temporary outputs. `check_example.py` is an optional local integration check requiring `results/inputs/tmp_single_input/Schnannerbach_extract1.docx` and a live API key; it replaces `results/example_check` when run.

Processing requires `TW_LLM_API_BASE_URL` and `TW_LLM_API_KEY` in the environment before starting the application. Use a compatible chat-completions service; the client appends `/chat/completions` to the base URL. Configure the model in `multi_hazard_pipeline/config.py`. 
