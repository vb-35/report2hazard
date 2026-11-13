# Interface code guide (for a non-programmer)

This document explains the code behind the **Multi-hazard review** interface in plain language. It is meant to be read next to the real source files. The source files remain the authoritative version; the shortened examples below are teaching copies with extra comments.

## 1. The big picture

The interface is a small local website. It runs only on this computer unless the program is deliberately reconfigured.

```text
You click "Start Interface.cmd"
        |
        v
launch_interface.py starts a local web server and opens the browser
        |
        v
web.py decides what each web address should do
        |
        +--> templates/*.html provide the page structure and wording
        +--> static/style.css controls appearance and layout
        +--> static/*.js makes pages interactive and refreshes live results
        |
        v
The pipeline writes results into results/<run-id>/
        |
        v
The browser repeatedly asks for new status/results and redraws the page
```

An easy way to think about the technologies is:

- **Python/Flask** is the receptionist. It receives requests, validates them, starts work, reads saved results, and sends a response.
- **HTML/Jinja** is the page skeleton. It declares headings, forms, buttons, tables, and panels.
- **CSS** is the visual designer. It controls colors, spacing, columns, scrolling, and responsive layout.
- **JavaScript** is the live assistant in the browser. It checks forms, polls for updates, fills tables, navigates citations, and submits review actions without losing the page state.
- **JSON files** are the saved records exchanged between the pipeline and the interface.

## 2. Where the interface code lives

| File | What it is responsible for |
| --- | --- |
| [`Start Interface.cmd`](Start%20Interface.cmd) | Windows double-click entry point. |
| [`launch_interface.py`](launch_interface.py) | Asks for the API key when needed, starts the local server on a free port, and opens the browser. |
| [`multi_hazard_pipeline/web.py`](multi_hazard_pipeline/web.py) | Main interface backend: page routes, upload validation, background work, review actions, downloads, and deletion. |
| [`multi_hazard_pipeline/templates/base.html`](multi_hazard_pipeline/templates/base.html) | Shared outer page used by all screens. |
| [`multi_hazard_pipeline/templates/index.html`](multi_hazard_pipeline/templates/index.html) | Home page: upload form and list of previous runs. |
| [`multi_hazard_pipeline/templates/collection.html`](multi_hazard_pipeline/templates/collection.html) | Progress page for a PDF containing several reports. |
| [`multi_hazard_pipeline/templates/run.html`](multi_hazard_pipeline/templates/run.html) | Detailed report-review workspace. |
| [`multi_hazard_pipeline/static/style.css`](multi_hazard_pipeline/static/style.css) | All colors, spacing, layouts, state indicators, dialogs, and small-screen rules. |
| [`multi_hazard_pipeline/static/upload.js`](multi_hazard_pipeline/static/upload.js) | Checks the upload form before it is sent. |
| [`multi_hazard_pipeline/static/collection.js`](multi_hazard_pipeline/static/collection.js) | Refreshes a collection's status and report table. |
| [`multi_hazard_pipeline/static/status.js`](multi_hazard_pipeline/static/status.js) | The largest browser file: live status, segment table, evidence reader, citations, statistics, and review actions. |
| [`multi_hazard_pipeline/static/edit.js`](multi_hazard_pipeline/static/edit.js) | Manages the segment editor and protects unsaved edits. |
| [`multi_hazard_pipeline/static/delete.js`](multi_hazard_pipeline/static/delete.js) | Opens and fills the permanent-deletion warning dialog. |
| [`templates/_model_options.html`](multi_hazard_pipeline/templates/_model_options.html), [`static/model-options.js`](multi_hazard_pipeline/static/model-options.js) | Provider, model and effort fields shared by the upload and localization forms. |
| [`agent_web.py`](multi_hazard_pipeline/agent_web.py), [`agent_runs.py`](multi_hazard_pipeline/agent_runs.py), [`templates/agent_*.html`](multi_hazard_pipeline/templates/agent_new.html), [`static/agent-run.js`](multi_hazard_pipeline/static/agent-run.js) | Agent runs: form, run page with live status and stop button, and the backend that starts Claude Code or Codex in a background thread. |
| [`multi_hazard_pipeline/workspace.py`](multi_hazard_pipeline/workspace.py) | Prepares saved pipeline information in the shape the browser expects. This supports the interface, although it is not visible page code. |

## 3. How the interface starts

The Windows command file launches `launch_interface.py`. The important part of the Python launcher is equivalent to this:

```python
def main():
    # Find the name of the environment variable where the API key belongs.
    key_name = DEFAULT_CONFIG.llm.api_key_env_var

    # If there is no key already, privately ask for one in the terminal.
    # Pressing Enter is allowed when the user only wants to inspect old results.
    if not os.environ.get(key_name, "").strip():
        key = getpass.getpass("API key: ").strip()
        if key:
            os.environ[key_name] = key

    # Build the Flask application and tell it to store/read runs in "results".
    app = create_app(Path(__file__).resolve().parent / "results")

    # 127.0.0.1 means "this computer only".
    # Port 0 asks Windows to choose a currently unused port.
    server = make_server("127.0.0.1", 0, app, threaded=True)

    # Open the interface in the normal web browser, then keep serving pages
    # until the user closes the terminal or presses Ctrl+C.
    webbrowser.open(f"http://127.0.0.1:{server.server_port}")
    server.serve_forever()
```

The final two lines in the real file are a standard Python safety pattern:

```python
if __name__ == "__main__":
    main()  # Run main() only when this file is launched directly.
```

## 4. The Python web server (`web.py`)

### 4.1 Constants: the interface's fixed rules

At the top of `web.py`, constants collect rules that should have one clear definition:

```python
# Only these report types can be selected.
SUPPORTED_SUFFIXES = {".docx", ".pdf", ".txt"}

# Only these generated files may be downloaded through the download address.
DOWNLOADS = {"candidate_report.json", "final_rows.json", "final_rows.csv"}

# Internal machine-friendly states are translated into readable labels.
STATUS_LABELS = {
    "running": "Running",
    "awaiting_human_review": "Awaiting human review",
    "approved": "Approved",
    # ...other states...
}
```

Keeping these rules near the top reduces mismatches and makes the allowed values easy to audit.

### 4.2 Safe run lookup

`_run_dir()` turns a run ID from a web address into a results folder. It deliberately rejects suspicious or nonexistent IDs.

```python
def _run_dir(app, run_id):
    root = Path(app.config["ARTIFACT_ROOT"])
    candidate = (root / run_id).resolve()

    # Reject blank IDs, path tricks such as "../", and paths outside results/.
    if not run_id or Path(run_id).name != run_id or candidate.parent != root:
        abort(404)

    # A real run must contain its main record.
    if not (candidate / "manifest.json").is_file():
        abort(404)
    return candidate
```

This is an important security boundary: text from a URL is never trusted as an unrestricted file path.

### 4.3 Background processing

Pipeline work can take a long time. `_submit()` sends that work to a background executor so the web request can return quickly and the progress page can appear.

```python
def _submit(app, task, run_dir, operation, *args, **kwargs):
    def work():
        try:
            operation(*args, **kwargs)  # Run the requested pipeline operation.
        except Exception as exc:
            # Save failure details so the page can show them instead of failing silently.
            _record_worker_failure(run_dir, task, exc)

    app.extensions["pipeline_executor"].submit(work)
```

The executor is intentionally created with `max_workers=1`. In plain language, the application processes one pipeline job at a time. Additional jobs wait in line, which prevents several expensive model workflows from colliding.

### 4.4 Creating the Flask application

`create_app()` assembles the application:

```python
app = Flask(__name__)                     # Create the web application.
root.mkdir(parents=True, exist_ok=True)   # Ensure the results folder exists.
app.config.update(
    ARTIFACT_ROOT=str(root),
    PIPELINE_CONFIG=config,
    MAX_CONTENT_LENGTH=100 * 1024 * 1024, # Refuse requests over 100 MB.
)
```

Flask then connects web addresses to Python functions. These connections are called **routes**.

| Method and address | Python function | Meaning |
| --- | --- | --- |
| `GET /` | `index()` | Show the upload form and previous runs. |
| `POST /runs` | `start_run()` | Validate an upload/folder and begin processing. |
| `GET /runs/<run_id>` | `run_detail()` | Show either one report or its collection page. |
| `GET /runs/<run_id>/status` | `run_status()` | Return a compact JSON status. |
| `GET /runs/<run_id>/collection` | `collection_status()` | Return live JSON for a collection page. |
| `GET /runs/<run_id>/workspace` | `run_workspace()` | Return changed report results and reader data. |
| `GET /runs/<run_id>/source/<document_id>` | `source_file()` | Open/download one allowlisted source document. |
| `POST /runs/<run_id>/decision` | `decide()` | Approve, reject, or request a correction. |
| `POST /runs/<run_id>/edit` | `edit_candidate()` | Save one candidate-field edit and re-evaluate it. |
| `POST /runs/<run_id>/delete` | `delete_run()` | Permanently remove a completed run and owned copies. |
| `GET /runs/<run_id>/download/<artifact>` | `download()` | Download an allowed result file. |

`GET` means “give me information.” `POST` means “I am sending information that may change something.” `<run_id>` is a placeholder filled with the actual run's identifier.

### 4.5 Starting a run

`start_run()` is the upload controller. Its logic is:

1. Read uploaded files, the optional folder path, and the selected report mode.
2. Require exactly one input method: uploaded files **or** a folder. Read the model choices; empty fields keep the defaults.
3. Clean uploaded filenames and allow only PDF, DOCX, or TXT.
4. For a multi-report collection, require exactly one PDF.
5. Create a saved run record.
6. Queue either the ordinary pipeline or the PDF-separation workflow.
7. Redirect the browser to the new progress page.

The model choices are saved in the run's manifest (`configuration.llm_provider`, `llm_model`, `reasoning_effort`), and `recorded_language_config()` reapplies them when the run is processed again.

### 4.5b Free agent runs

The **Agent run** page (`/agent-runs/new`) hands the mapping prompt to Claude Code or Codex through `segment_mapping/agent_launch.py`, in its own thread. The run is laid out like a pipeline localization (`RUN/localization`, logs in `RUN/localization/agent/`), so Compare lists it with the pipeline runs of the same segments. If the interface crashes while an agent works, the run is marked as interrupted at the next start and the agent may still be running (check Task Manager for `claude` or `codex`).

Both JavaScript and Python validate the form. Browser validation gives quick feedback; server validation is still essential because browser checks can be bypassed.

### 4.6 Multi-report collections

`_prepare_collection()` handles a PDF that contains several reports:

```python
# Mark the parent collection as being prepared.
manifest["stages"]["preparation"] = "running"

# Split the source PDF into separate report files.
split_paths = split_event_reports(source, destination, client, config)

# Give every separated report its own normal run and remember it as a child.
for path in split_paths:
    child = create_run(...)
    child["parent_run_id"] = manifest["run_id"]
    manifest["child_runs"].append(...)

# Process the child reports one at a time.
for child in manifest["child_runs"]:
    execute_run(...)
```

The collection page does not merge all results into one report. It acts as a parent dashboard linking to normal review pages for each separated child report.

### 4.7 Human decisions and edits

The `decide()` route separates three actions:

- **Approve:** validate the finished candidate and create authoritative exports.
- **Reject:** record the rejection and comments; no approved export is offered.
- **Request correction:** validate the requested stage/segment IDs, mark the run as active, and queue the correction.

The `edit_candidate()` route changes a single field in a selected segment, invalidates later results that may now be stale, and queues a fresh self-evaluation. Validation happens before the background task is accepted.

### 4.8 Downloads and source-file safety

The interface never accepts an arbitrary file path for downloading:

- Result downloads must be one of the names in `DOWNLOADS`.
- Final CSV/JSON files are available only after approval.
- Source documents are selected by a numeric index into the run's own input list.
- The source must still exist and have an allowed file extension.

These checks stop a crafted address from being used to read unrelated files.

### 4.9 Deletion

`_delete_run()` checks that processing has stopped, includes child reports when deleting a collection, and removes only input copies owned by the interface and unused by another run. `_remove_folder()` deletes `manifest.json` last. That detail is intentional: if deletion is interrupted, the run remains visible so deletion can be tried again.

## 5. HTML templates: the visible page structure

The templates use **Jinja**, which is HTML with placeholders and simple logic. Jinja expressions use braces:

```html
<!-- Insert a value. Jinja escapes unsafe HTML characters by default. -->
<h1>{{ collection.title }}</h1>

<!-- Include something only when a condition is true. -->
{% if error %}<p role="alert">{{ error }}</p>{% endif %}

<!-- Repeat markup for every item in a list. -->
{% for run in runs %}<a href="...">{{ run.title }}</a>{% endfor %}
```

### 5.1 `base.html`

This is the shared wrapper. Other templates “extend” it and fill named slots:

```html
<!doctype html>
<html lang="en">
<head>
  <!-- Character encoding and phone-sized layout support. -->
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">

  <!-- A child page may replace the title block. -->
  <title>{% block title %}Multi-hazard review{% endblock %}</title>
  <link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}">
</head>
<body>
  <header>...</header>
  <main>{% block content %}{% endblock %}</main>
  <footer>...</footer>
  {% block scripts %}{% endblock %}
</body>
</html>
```

This prevents every page from repeating the same outer HTML and stylesheet link.

### 5.2 `index.html`

The home page has two main areas:

- **Start a new report:** choose single/multi mode, files or a folder, then submit.
- **Your reports:** cards for saved runs with status and progress.

The form uses `enctype="multipart/form-data"`, which is required for sending files. The file input uses `multiple`, so one single report may be supplied as several related files. The permanent-delete dialog is also defined here, but stays closed until `delete.js` opens it.

Accessibility details include associated labels, `role="alert"` for errors, `aria-live` for changing status, table headers, and keyboard-focus styling.

### 5.3 `collection.html`

This page contains:

- overall collection status and progress;
- three broad stages: separation, extraction, and review;
- warnings/errors;
- a table containing every separated report;
- a “Review next report” shortcut.

It embeds the first server response as JSON:

```html
<script id="collection-data" type="application/json">
  {{ collection | tojson }}
</script>
```

This lets JavaScript draw immediately from the same data used by the HTML, then request newer data later.

### 5.4 `run.html`

This is the main review workspace. Its important sections are:

1. **Workspace header:** run status, current pipeline stage, correction count, diagnostics, and statistics button.
2. **Segments and evidence pane:** searchable/filterable results table plus the selected segment's details, evidence, classifications, and editor.
3. **Report reader pane:** original extracted text, translation, side-by-side comparison, or original PDF.
4. **Human review area:** comments, correction routing, approve/reject buttons, and downloads.
5. **Statistics dialog:** timing, model requests, retries, timeouts, and step history.

The page also embeds its initial `workspace` JSON. Most table rows and passages are then created by JavaScript instead of being written permanently into the HTML.

## 6. CSS: appearance and layout

CSS selectors describe which elements receive a visual rule:

```css
/* Every element with class="state-approved" gets a green left border. */
.state-approved { border-left-color: #16803a; }

/* Split the central workspace into two flexible columns. */
.workspace-panes {
  display: grid;
  grid-template-columns: minmax(0, 1.05fr) minmax(0, 1fr);
}

/* Keep the table header visible while its body scrolls. */
#segment-table th {
  position: sticky;
  top: 0;
}
```

The stylesheet is organized roughly as follows:

- Lines near the top define base colors, typography, buttons, panels, and messages.
- The “Review workspace” section creates the full-height two-pane layout and independent scrolling areas.
- Media queries change the layout for narrower screens.
- Later sections style report-mode choices, collections, live running indicators, statistics, and the deletion dialog.

The `[hidden]` rule is particularly important. JavaScript changes an element's `hidden` property to show or hide controls depending on the run state.

## 7. JavaScript: live behaviour in the browser

### 7.1 Upload validation (`upload.js`)

When the form is submitted, this script checks:

- a report mode was chosen;
- the user chose files or a folder, but not both;
- a multi-report upload is exactly one PDF;
- uploaded files total less than 100 MB.

If valid, it disables the submit button to prevent double submission and shows an upload message. The server repeats the important checks for safety.

### 7.2 Collection refresh (`collection.js`)

The file is wrapped in an immediately invoked function:

```javascript
(() => {
  // Variables inside here do not leak into unrelated scripts.
  // ...collection-page code...
})();
```

`render(data)` replaces the displayed status, stages, diagnostics, and table rows with the latest values. `poll()` requests fresh JSON, renders it, and schedules itself again:

```javascript
async function poll() {
  try {
    const response = await fetch(root.dataset.url, {
      cache: 'no-store',                 // Do not reuse an old browser copy.
      signal: AbortSignal.timeout(12000) // Give up after 12 seconds.
    });
    render(await response.json());
  } catch (error) {
    // Keep the last good display and tell the user that retrying will continue.
  }

  // Check rapidly during processing and slowly after it ends.
  setTimeout(poll, active ? 1500 : 5000);
}
```

Using `setTimeout` after a request finishes avoids piling up requests when a response is slow.

### 7.3 Main workspace controller (`status.js`)

This script holds the current browser-side `state` and redraws parts of the page when new data arrives.

Its main responsibilities are:

- `renderHeader()` — statuses, stages, timers, warnings, action availability, and download visibility.
- `renderTable()` — search/filter controls and keyboard-accessible segment rows.
- `renderDetails()` — the selected event, process, predecessors, classifications, evidence, and issues.
- `renderStatistics()` — processing totals, stage comparison bars, and attempt history.
- `renderEvaluation()` — automatic evaluation plus the human edit/decision audit trail.
- `renderReader()` — source document selection and original/translation/compare/PDF modes.
- `navigateCitation()` — connect an evidence quotation to its report passage and PDF page.
- `refresh()` / `poll()` — request changes every 1.5 seconds and preserve the last good display during temporary errors.
- `submit()` — send edits and review decisions in the background, report errors, and refresh the page state.

The browser tells the server which file versions it already has:

```javascript
const url = `${root.dataset.url}?since=${encodeURIComponent(
  JSON.stringify(state.versions || {})
)}`;
```

The server can then omit unchanged heavy data. This reduces repeated transfer and redrawing while still keeping the page current.

Citation highlighting carefully converts a JavaScript string to Unicode code points before applying Python-generated character offsets. Without that conversion, some emoji or less-common characters could shift the highlight.

### 7.4 Editing (`edit.js`)

The editor switches between:

- a normal text/number input for free-form fields; and
- a drop-down list for controlled classification labels.

It tracks whether the user has started an unsaved edit. Live polling may update everything else, but it does not overwrite the draft. If the selected segment or revision changes, the script marks the draft as stale and requires it to be discarded before submission. It also asks for confirmation before leaving the page with unsaved work.

### 7.5 Deletion (`delete.js`)

Each delete button stores its run-specific URL, title, folder, and collection size in `data-*` attributes. Clicking it copies those values into the shared warning dialog. Only submitting that dialog sends the permanent-delete request. The submit button is disabled after the first click to prevent duplicate requests.

## 8. A complete user journey through the code

### Upload and processing

1. The user opens the home page: `GET /` calls `index()` and renders `index.html`.
2. `upload.js` checks the form in the browser.
3. The form sends files/settings to `POST /runs`.
4. `start_run()` validates and saves the input, creates a manifest, and queues work.
5. Flask redirects to `GET /runs/<run_id>`.
6. `run.html` shows the workspace using the currently saved data.
7. `status.js` calls `/workspace` every 1.5 seconds.
8. As the pipeline writes newer files, `workspace.py` exposes them and the browser redraws the affected sections.

### Review and approval

1. When processing reaches human review, `renderHeader()` enables the valid action buttons.
2. Selecting a segment updates its details and moves the reader to cited evidence.
3. An edit goes to `POST /edit`, which saves the change and queues re-evaluation.
4. A correction request goes to `POST /decision`, which sends work back to the chosen stage.
5. Approval creates final outputs and changes the run status to `approved`.
6. `status.js` reveals the CSV and JSON download links only after approval.

## 9. Where to make common changes

| Desired change | Usually edit here |
| --- | --- |
| Change visible wording or add a form control | The relevant file in `templates/` |
| Change colors, spacing, columns, or responsive layout | `static/style.css` |
| Change live table/reader behaviour | `static/status.js` |
| Change upload checks shown immediately | `static/upload.js` **and** matching validation in `web.py` |
| Add a new web action or address | `web.py`, usually with matching HTML/JavaScript |
| Change allowed upload/download formats | Constants and validation in `web.py`, plus the HTML file input if uploads change |
| Change what live data is sent to the browser | `workspace.py` and the matching renderer in `status.js` |
| Add a model provider or change its default model | `PROVIDERS` in `config.py`; the forms pick it up automatically |
| Change how agent runs start (flags, permissions) | `segment_mapping/agent_launch.py` |
| Change pipeline processing itself | Pipeline/agent modules, not primarily the interface files |

## 10. Terms used in the code

- **Route:** a web address connected to a Python function.
- **Request:** a message from the browser to the server.
- **Response:** what the server sends back (HTML, JSON, a file, or an error).
- **Template:** HTML containing placeholders filled by Python.
- **Manifest:** `manifest.json`, the main saved status record for a run.
- **Artifact:** a generated result file such as candidate JSON or final CSV.
- **DOM:** the browser's in-memory representation of the page. JavaScript changes it to redraw the interface.
- **Polling:** asking the server for updates repeatedly at a controlled interval.
- **Async/background work:** processing that continues without freezing the displayed page.
- **Validation:** checking that input is permitted and sensible before using it.
- **Escaping:** displaying user-controlled text as text rather than executable HTML.
- **Audit trail:** a saved history of edits and review decisions.

## 11. Important design choices worth preserving

- The server binds to `127.0.0.1`, keeping the default interface local to this computer.
- Server-side validation remains even when JavaScript performs the same friendly check.
- Run IDs and download names are allowlisted instead of becoming arbitrary paths.
- Pipeline failures are recorded in the manifest so they remain visible and diagnosable.
- Only one long-running pipeline job executes at a time.
- Polling preserves the last successfully displayed data during temporary read/connection errors.
- Unsaved edits and review comments are protected from live refreshes and accidental navigation.
- Final exports stay hidden and unavailable until approval.
- Deletion is blocked during processing and removes only owned, unused input copies.
