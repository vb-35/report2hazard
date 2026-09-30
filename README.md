# Multi-hazard report pipeline

The pipeline extracts a single qualitative report, translates every chunk to normalized English, builds one ordered causal chain, categorizes every segment, evaluates the complete candidate, and pauses for a real human decision. Authoritative `final_rows.json` and `final_rows.csv` files are created only after approval.

Workflow: extraction → translation → segmentation → categorization → candidate report → self-evaluation/correction → human review → final export.

## Report grouping

One run contains only files explicitly selected together as companion parts or supplements of one logical report. The pipeline does not guess relationships from filenames. Put unrelated reports in separate input directories or upload them as separate runs. This rule and the selected files are recorded in `source.json` and `manifest.json`.

Supported inputs are DOCX, PDF, and UTF-8 TXT files containing English, German, French, or Italian text. `source.json` remains the authoritative extraction. `translated.json` preserves every original chunk and its provenance and adds `source_language` and `translated_text`; English chunks pass through without semantic rewriting. Segmentation interprets the English translation while evidence quotes remain exact or whitespace-normalized substrings of the original text. Unchanged causal steps retain their segment IDs across revisions; newly introduced steps receive new IDs independently of causal order.

## Easy interface (Windows)

Before processing reports, set `TW_LLM_API_BASE_URL` in your Windows user environment to your provider's API base URL (including its API version path), then open a new launcher window. No service address is bundled. Browsing existing results does not require this setting.

Requires Python 3.10 or newer. From the project folder, create an environment and install dependencies:

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

This version intentionally uses a single-process, in-memory worker. Restarting the server loses queued/running jobs, while completed artifact directories remain reopenable. Do not enable Flask's development reloader because it can duplicate the worker. The UI has no authentication or CSRF layer and is intended only for localhost; do not bind it to an untrusted network.

The correction maximum is run-wide, including automatic and human-requested reruns. A translation correction reruns translation, segmentation, and categorization; a segmentation correction also reruns categorization. Whole-report evaluation receives each chunk's original and English text so it can check bilingual fidelity and the complete causal chain.

## Run states and artifacts

Run folders use the input report name followed by the UTC start time, for example `schnannerbach__2026-09-28_08-36-16-123456Z`. The final six digits are microseconds to distinguish closely spaced runs. Companion-file runs use the first selected file's name; split collections use each event report's filename.

Statuses are `running`, `revision_required`, `awaiting_human_review`, `rejected`, `failed`, and `approved`.

Each run directory preserves `source.json`, `translated.json`, `segments.json`, `classified.json`, `candidate_report.json`, `self_evaluation.json`, `human_review.json`, and `manifest.json`. Only approved runs add authoritative `final_rows.json` and `final_rows.csv`.

Run deterministic tests without an API key:

```powershell
python -m pytest -q
```

OCR, geospatial/GIS support, and external knowledge retrieval are deliberately deferred. Scanned-PDF OCR is not supported, and Tesseract `.traineddata` files are not required.

## Repository contents and local data

The repository includes the application, tests, and the required `Translation resources/multi_hazard_keywords.csv` glossary. Keep that CSV at its existing path.

All local outputs belong in `results/`, which is excluded by `.gitignore`. The launcher and CLI default to this folder; PDF splitting defaults to `results/split/`. Explicit CLI output directories remain supported. Preserved source documents live in `results/inputs/`, spreadsheet exports in `results/exports/`, and supporting logs and split inputs in `results/run_support/`. Run directories are directly inside `results/` so the review interface can find them. Obsolete local files are collected in `results/obsolete/`. Python environments and caches are also ignored.

The test suite uses synthetic inputs. One optional PDF regression test is skipped when `results/inputs/FAI/Example_Complete/Ereignisdokumentation2018.pdf` is absent. `check_example.py` is a local integration check requiring `results/inputs/tmp_single_input/Schnannerbach_extract1.docx` and a live API key; it replaces `results/example_check` when run.

Processing requires `TW_LLM_API_BASE_URL` and `TW_LLM_API_KEY` in the environment before starting the application. Use a compatible chat-completions service; the client appends `/chat/completions` to the base URL. Configure the model in `multi_hazard_pipeline/config.py`. 
