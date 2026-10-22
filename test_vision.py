"""Offline Responses API and selected-page vision checks: python -m unittest test_vision."""
import base64
import json
from dataclasses import replace
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from urllib import request

from PIL import Image

from multi_hazard_pipeline import cli
from multi_hazard_pipeline.config import DEFAULT_CONFIG
from multi_hazard_pipeline.errors import PipelineError
from multi_hazard_pipeline.llm import ChatClient, extract_response_json
from multi_hazard_pipeline.vision import VISION_SCHEMA, analyze_image, load_images
from test_llm import Response


def response(text, **extra):
    return {"status": "completed", "output": [
        {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "not the answer",
                                             "parsed": {"wrong": True}}]},
        {"type": "message", "role": "assistant", "status": "completed",
         "content": [{"type": "output_text", "text": text}]},
    ], **extra}


class VisionTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "picture.png"
        with Image.new("RGB", (600, 300), "red") as picture:
            picture.save(self.path)
        self.client = ChatClient("offline", replace(DEFAULT_CONFIG.llm,
            api_base_url="https://offline.invalid/v1", text_endpoint="/responses", max_output_tokens=4096))
        self.network = patch.object(request, "urlopen", side_effect=AssertionError("unexpected network"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def test_responses_text_and_status(self):
        self.assertEqual(extract_response_json(response('{"ok":true}')), {"ok": True})
        payload = response('{"ok":true}')
        payload["output"].insert(0, {"type": "message", "role": "tool",
                                     "content": [{"parsed": {"wrong": True}}]})
        self.assertEqual(extract_response_json(payload), {"ok": True})
        for status in ("incomplete", "failed", "in_progress", "cancelled"):
            with self.assertRaises(ValueError):
                extract_response_json(response('{"ok":true}', status=status))
        with self.assertRaises(PipelineError):
            extract_response_json({"status": "completed", "output": [
                {"type": "reasoning", "content": [{"type": "reasoning_text", "text": '{"ok":true}'}]}]})
        with self.assertRaises(ValueError):
            extract_response_json(response("unused", output=[{"type": "message", "role": "assistant",
                "content": [{"type": "refusal", "refusal": "cannot answer"}]}]))
        with self.assertRaises(ValueError):
            extract_response_json(response('{"ok":true} trailing garbage'))

    def test_request_and_repair_keep_images(self):
        invalid = response('{"answer":42,"uncertainties":[]}')
        valid = response('{"answer":"red rectangle","uncertainties":[]}')
        with patch.object(request, "urlopen", side_effect=[Response(invalid), Response(valid)]) as transport:
            result = analyze_image(self.client, self.path, question="What is visible?")
        bodies = [json.loads(call.args[0].data) for call in transport.call_args_list]
        first, second = bodies
        self.assertEqual(transport.call_args.args[0].full_url, "https://offline.invalid/v1/responses")
        self.assertEqual(first["input"], second["input"][:2])
        self.assertEqual(first["text"]["format"], {"type": "json_schema", **VISION_SCHEMA})
        self.assertEqual(first["text"], second["text"])
        self.assertEqual(first["max_output_tokens"], 4096)
        self.assertEqual(first["reasoning"], {"effort": "medium"})
        self.assertFalse(first["store"])
        self.assertFalse({"messages", "max_tokens", "reasoning_effort", "response_format"} & first.keys())
        self.assertEqual(first["input"][1]["content"][1]["type"], "input_image")
        self.assertEqual(second["input"][2]["content"], '{"answer":42,"uncertainties":[]}')
        self.assertNotIn("not the answer", json.dumps(second))
        self.assertEqual(result["sources"][0]["filename"], "picture.png")
        self.assertEqual(result["answer"], "red rectangle")

    def test_text_only_responses(self):
        body = json.loads(self.client._request_body(system_prompt="Return JSON", user_payload={"hello": "world"},
                                                  response_schema=None))
        self.assertIsInstance(body["input"][1]["content"], str)
        self.assertNotIn("text", body)

    def test_size_limits_and_repair_truncation(self):
        images, _ = load_images(self.path)
        task = dict(system_prompt="Return JSON", user_payload={}, response_schema=VISION_SCHEMA, images=images)
        original = self.client._request_body(**task)
        self.client.config = replace(self.client.config, max_request_chars=len(original) + 600)
        body = self.client._request_body(**task, repair={"kind": "parsing_error", "error": "bad JSON",
                                                       "answer": "invalid" * 1000})
        self.assertLessEqual(len(body), self.client.config.max_request_chars)
        self.assertEqual(json.loads(original)["input"], json.loads(body)["input"][:2])
        self.assertTrue(json.loads(body)["input"][2]["content"].endswith("[failed answer truncated]"))
        self.client.config = replace(self.client.config, max_request_chars=100)
        with self.assertRaisesRegex(PipelineError, "max_request_chars"):
            self.client._request_body(**task)

    def test_images_require_responses_and_data_urls(self):
        task = dict(system_prompt="Check", user_payload={}, response_schema=None)
        with self.assertRaisesRegex(PipelineError, "base64 data URLs"):
            self.client._request_body(**task, images=["file:///private.png"])
        self.client.config = replace(self.client.config, text_endpoint="/chat/completions")
        with self.assertRaisesRegex(PipelineError, "/responses"):
            self.client._request_body(**task, images=load_images(self.path)[0])

    def test_picture_resize_and_transparency(self):
        with Image.new("RGBA", (600, 300), (0, 0, 0, 0)) as picture:
            picture.save(self.path)
        images, sources = load_images(self.path, max_dimension=256)
        self.assertEqual((sources[0]["width"], sources[0]["height"]), (256, 128))
        with Image.open(BytesIO(base64.b64decode(images[0].split(",", 1)[1]))) as picture:
            self.assertEqual(picture.getpixel((0, 0)), (255, 255, 255))
        with self.assertRaisesRegex(PipelineError, "only to PDF"):
            load_images(self.path, [1])
        with self.assertRaisesRegex(PipelineError, "256 and 4096"):
            load_images(self.path, max_dimension=10000)

    def test_pdf_selection_order_and_bounds(self):
        pdf = self.root / "pages.pdf"
        with Image.new("RGB", (200, 100), "red") as first, Image.new("RGB", (100, 200), "blue") as second:
            first.save(pdf, save_all=True, append_images=[second])
        images, sources = load_images(pdf, [2, 1], max_dimension=256)
        self.assertEqual([s["page"] for s in sources], [2, 1])
        self.assertEqual([(s["width"], s["height"]) for s in sources], [(128, 256), (256, 128)])
        with Image.open(BytesIO(base64.b64decode(images[0].split(",", 1)[1]))) as picture:
            self.assertGreater(picture.getpixel((60, 60))[2], 240)
        for pages in (None, [], [0], [3], [1, 1], list(range(1, 10))):
            with self.assertRaises(PipelineError):
                load_images(pdf, pages)

    def test_cli_routes_to_working_endpoint_and_preserves_output(self):
        output = self.root / "analysis.json"
        with patch.object(ChatClient, "from_config") as make_client, \
             patch("multi_hazard_pipeline.vision.analyze_image", return_value={"answer": "red"}) as analyze, \
             patch("sys.stdout", new_callable=StringIO):
            self.assertEqual(cli.main(["analyze-image", str(self.path), "--output", str(output)]), 0)
            config = make_client.call_args.args[0]
            self.assertEqual(config.api_base_url, "http://llm.igf.oeaw.ac.at/v1")
            self.assertEqual(config.text_endpoint, "/responses")
            self.assertEqual(config.max_output_tokens, 4096)
            analyze.reset_mock()
            with patch("sys.stderr", new_callable=StringIO):
                self.assertEqual(cli.main(["analyze-image", str(self.path), "--output", str(output)]), 1)
            analyze.assert_not_called()
        self.assertEqual(json.loads(output.read_text()), {"answer": "red"})
