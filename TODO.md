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
