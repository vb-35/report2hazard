"""Interface: model provider choices for extraction/localization, and free agent runs (Claude Code or Codex)."""
import io
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from test_localization_stage import FakeModel, make_package, make_pipeline_run

TABLE = ("Segment\tEvent\tProcess\tGeneralized Category\tInteraction Type\tSediment Transport Phase\n"
         "1\tTestbach\tHeavy rain over the catchment\tTriggering Event\tProcess-process\tnot applicable\n")


class QueuedExecutor:
    def __init__(self):
        self.tasks = []

    def submit(self, task):
        self.tasks.append(task)


def which(name):
    return f"/bin/{name}"


class ProviderChoiceTests(TestCase):
    def setUp(self):
        from multi_hazard_pipeline import web
        from multi_hazard_pipeline.config import DEFAULT_CONFIG, LLMConfig

        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.reports = self.tmp / "reports"
        self.reports.mkdir()
        (self.reports / "report.txt").write_text("Heavy rain mobilized sediment.", encoding="utf-8")
        config = replace(DEFAULT_CONFIG, llm=LLMConfig(provider="openai", api_base_url="http://router/v1"))
        self.executor = QueuedExecutor()
        self.app = web.create_app(self.tmp / "results", config=config, executor=self.executor)
        self.app.testing = True
        self.client = self.app.test_client()

    def test_index_offers_providers_and_agent_runs(self):
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('name="provider"', page)
        self.assertIn("Anthropic API (Claude)", page)
        self.assertIn('name="effort"', page)
        self.assertIn("/agent-runs/new", page)

    def test_extraction_records_the_chosen_provider_for_later_steps(self):
        from multi_hazard_pipeline.core import read_json
        from multi_hazard_pipeline.language import recorded_language_config
        from multi_hazard_pipeline.config import DEFAULT_CONFIG

        response = self.client.post("/runs", data=dict(report_mode="single", input_dir=str(self.reports),
                                                       provider="anthropic", effort="high"))
        self.assertEqual(response.status_code, 303, response.get_data(as_text=True)[:500])
        [manifest_path] = (self.tmp / "results").glob("*/manifest.json")
        manifest = read_json(manifest_path)
        saved = manifest["configuration"]
        self.assertEqual((saved["llm_provider"], saved["llm_model"], saved["reasoning_effort"]),
                         ("anthropic", "claude-opus-5-5", "high"))
        llm = recorded_language_config(DEFAULT_CONFIG, manifest).llm
        self.assertEqual((llm.provider, llm.model, llm.reasoning_effort), ("anthropic", "claude-opus-5-5", "high"))
        run_page = self.client.get(f"/runs/{manifest['run_id']}").get_data(as_text=True)
        self.assertIn("Anthropic API (Claude): claude-opus-5-5 (high effort)", run_page)

    def test_unknown_choices_are_refused(self):
        for data in (dict(provider="gemini"), dict(effort="extreme")):
            response = self.client.post("/runs", data=dict(report_mode="single", input_dir=str(self.reports), **data))
            self.assertEqual(response.status_code, 400)
        self.assertFalse(list((self.tmp / "results").glob("*/manifest.json")))

    def test_old_runs_without_a_recorded_provider_keep_the_current_one(self):
        from multi_hazard_pipeline.config import DEFAULT_CONFIG
        from multi_hazard_pipeline.language import recorded_language_config

        self.assertIs(recorded_language_config(DEFAULT_CONFIG, {"configuration": {}}).llm, DEFAULT_CONFIG.llm)


class AgentRunTests(TestCase):
    @classmethod
    def setUpClass(cls):
        from multi_hazard_pipeline.agents.localization_agent import localize_run
        from multi_hazard_pipeline.core import read_json, write_json

        cls.tmp = Path(tempfile.mkdtemp()).resolve()
        cls.results = cls.tmp / "results"
        cls.results.mkdir()
        pkg = make_package(cls.tmp / "package")
        run = make_pipeline_run(cls.results)
        manifest = read_json(run / "manifest.json")
        manifest.update(run_id=run.name, event="Testbach", created_at="2026-11-10T09:00:00+00:00")
        for item in manifest["inputs"]:  # real runs record the file name the reports list shows
            item.setdefault("filename", Path(item["path"]).name)
        write_json(run / "manifest.json", manifest)
        localize_run(run, pkg, client=FakeModel(), vision=False, browser_check=False, log=lambda m: None)
        cls.base = run
        cls.pdf = cls.tmp / "report.pdf"
        cls.pdf.write_bytes(b"%PDF-1.4 offline placeholder")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        from multi_hazard_pipeline import web

        self.app = web.create_app(self.results, executor=QueuedExecutor())
        self.app.testing = True
        self.client = self.app.test_client()
        patcher = patch("multi_hazard_pipeline.agent_runs.shutil.which", which)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        for path in self.results.iterdir():
            if path != self.base:
                shutil.rmtree(path, ignore_errors=True)

    def fake_launch(self, exit_code=0, deliver=True):
        """Stands in for the agent: copies a finished toolkit run into the agent's run folder."""
        def launch(args, prompt, *, log, on_start=None):
            target = Path(args.run)
            assert "Testbach" in prompt or "Heavy rain" in prompt
            if deliver:
                shutil.copytree(self.base / "localization", target, dirs_exist_ok=True)
            (target / "agent").mkdir(parents=True, exist_ok=True)
            (target / "agent" / "prompt.txt").write_text(prompt, encoding="utf-8")
            (target / "agent" / "transcript.jsonl").write_text('{"type":"result"}\n', encoding="utf-8")
            log("[claude] tool Bash: python -m segment_mapping build RUN")
            return {"exit_code": exit_code}
        return launch

    def start(self, launch, **form):
        from multi_hazard_pipeline import agent_runs

        threads = []
        original = agent_runs.start_agent_run

        def start_and_wait(run_dir, log_hook=None):
            thread = original(run_dir, log_hook)
            threads.append(thread)
            thread.join(30)
            return thread

        data = dict(runner="claude", model="claude-opus-5-5", effort="high", report_path=str(self.pdf),
                    segments_file=(io.BytesIO(TABLE.encode()), "segments.tsv"))
        data.update(form)
        with patch("segment_mapping.agent_launch.launch", launch), \
                patch("multi_hazard_pipeline.agent_web.agent_runs.start_agent_run", start_and_wait):
            response = self.client.post("/agent-runs/new", data=data, content_type="multipart/form-data")
        return response

    def test_form_lists_agents_and_warns_about_blind_runs_in_the_project(self):
        page = self.client.get("/agent-runs/new").get_data(as_text=True)
        self.assertIn("Claude Code", page)
        self.assertIn("Codex", page)
        self.assertIn('name="full_access"', page)

    def test_delivered_agent_run_is_listed_and_compared_with_the_pipeline_run(self):
        from multi_hazard_pipeline.core import read_json
        from multi_hazard_pipeline.localization_compare import comparable_groups, comparison_data, export_runs

        response = self.start(self.fake_launch())
        self.assertEqual(response.status_code, 303, response.get_data(as_text=True)[:800])
        run_id = response.headers["Location"].rstrip("/").split("/")[-1]
        manifest = read_json(self.results / run_id / "manifest.json")
        self.assertEqual(manifest["status"], "completed", manifest.get("activity"))
        self.assertEqual(manifest["agent"]["model"], "Claude Code · claude-opus-5-5 · high effort")
        self.assertEqual(read_json(self.results / run_id / "localization" / "run.json")["model"],
                         "Claude Code · claude-opus-5-5 · high effort")
        groups = [g for g in comparable_groups(self.results) if any(r["run_id"] == run_id for r in g["runs"])]
        self.assertEqual(len(groups), 1)
        kinds = {r["run_id"]: r["kind"] for r in groups[0]["runs"]}
        self.assertEqual(kinds[run_id], "agent")
        self.assertEqual(kinds[self.base.name], "pipeline")
        data = comparison_data(self.results, [self.base.name, run_id])
        self.assertEqual(set(data["reports"][0]["runs"]), {self.base.name, run_id})
        archive, _ = export_runs(self.results, [run_id])
        self.assertTrue(archive)

        page = self.client.get(f"/agent-runs/{run_id}").get_data(as_text=True)
        self.assertIn("Map delivered", page)
        self.assertIn("Compare with other runs", page)
        self.assertIn("segments_map.html", page)
        status = self.client.get(f"/agent-runs/{run_id}/status").get_json()
        self.assertEqual(status["status"], "completed")
        self.assertIn("transcript.jsonl", {item["name"] for item in status["logs"]})
        self.assertTrue(any("tool Bash" in item["message"] for item in status["log"]))
        self.assertEqual(self.client.get(f"/agent-runs/{run_id}/files/prompt.txt").status_code, 200)
        self.assertEqual(self.client.get(f"/runs/{run_id}").status_code, 302)
        index = self.client.get("/").get_data(as_text=True)
        self.assertIn("Free agent run", index)

    def test_agent_without_a_map_fails_visibly(self):
        from multi_hazard_pipeline.core import read_json

        response = self.start(self.fake_launch(deliver=False))
        run_id = response.headers["Location"].rstrip("/").split("/")[-1]
        manifest = read_json(self.results / run_id / "manifest.json")
        self.assertEqual(manifest["status"], "failed")
        self.assertIn("without delivering a map", manifest["activity"])

    def test_approved_report_prefills_segments_and_rejects_non_pdf_reports(self):
        # The pipeline test run's report is a text file: the agent needs a PDF, so the form asks for one.
        response = self.start(self.fake_launch(), report_path="", segments_file=(io.BytesIO(b""), ""),
                              from_run=self.base.name)
        self.assertEqual(response.status_code, 409)
        self.assertIn("PDF", response.get_data(as_text=True))
        response = self.start(self.fake_launch(), segments_file=(io.BytesIO(b""), ""), from_run=self.base.name)
        self.assertEqual(response.status_code, 303, response.get_data(as_text=True)[:800])

    def test_invalid_choices_leave_nothing_behind(self):
        before = set(self.results.iterdir())
        for form in (dict(runner="cursor"), dict(effort="max", runner="codex"), dict(report_path=str(self.tmp / "missing.pdf"))):
            response = self.start(self.fake_launch(), **form)
            self.assertEqual(response.status_code, 409, form)
        self.assertEqual(set(self.results.iterdir()), before)


class AgentStateTests(TestCase):
    """Terminal states are recorded under the run lock, whatever the agent leaves behind."""

    def setUp(self):
        from multi_hazard_pipeline.pipeline import save_manifest

        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.run = self.tmp / "run"
        (self.run / "localization").mkdir(parents=True)
        save_manifest(self.run, dict(run_id="run", kind="agent_localization", status="running",
                                     agent=dict(launched=True, runner="claude", model="Claude Code · m")))

    def manifest(self):
        from multi_hazard_pipeline.core import read_json
        return read_json(self.run / "manifest.json")

    def test_malformed_toolkit_config_still_finishes_the_run(self):
        from multi_hazard_pipeline import agent_runs

        (self.run / "localization" / "run.json").write_text("{not json", encoding="utf-8")
        agent_runs._finish(self.run, {"exit_code": 0}, None)
        manifest = self.manifest()
        self.assertEqual(manifest["status"], "failed")
        self.assertIn("run.json could not be labelled", " ".join(manifest["warnings"]))

    def test_stop_after_the_agent_finished_is_refused_and_keeps_the_result(self):
        from multi_hazard_pipeline import agent_runs
        from multi_hazard_pipeline.errors import PipelineError

        agent_runs._PROCESSES["run"] = SimpleProcess()
        agent_runs._finish(self.run, {"exit_code": 1}, None)
        self.assertNotIn("run", agent_runs._PROCESSES)
        with self.assertRaises(PipelineError):
            agent_runs.stop_agent_run(self.run)
        self.assertEqual(self.manifest()["status"], "failed")

    def test_stopped_run_is_recorded_as_stopped(self):
        from multi_hazard_pipeline import agent_runs

        process = SimpleProcess()
        agent_runs._PROCESSES["run"] = process
        with patch("segment_mapping.agent_launch.stop_process") as stop:
            agent_runs.stop_agent_run(self.run)
        stop.assert_called_once_with(process)
        self.assertEqual(self.manifest()["status"], "stopping")
        agent_runs._finish(self.run, {"exit_code": 1}, None)
        self.assertEqual(self.manifest()["status"], "stopped")


class SimpleProcess:
    pid = 4242

    def poll(self):
        return None
