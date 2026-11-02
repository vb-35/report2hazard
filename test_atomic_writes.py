"""Offline regression tests for Windows locks during atomic artifact writes."""
import errno
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from multi_hazard_pipeline import core


def windows_error(code):
    error = PermissionError(errno.EACCES, "File temporarily locked")
    error.winerror = code
    return error


class AtomicWriteTests(unittest.TestCase):
    def test_transient_windows_locks_keep_previous_json_until_replacement(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            core.write_json(path, {"status": "preparation"})
            real_replace = os.replace
            calls = []

            def locked_replace(source, destination):
                calls.append((source, destination))
                self.assertEqual(core.read_json(path), {"status": "preparation"})
                if len(calls) <= 3:
                    raise windows_error((5, 32, 33)[len(calls) - 1])
                return real_replace(source, destination)

            with patch.object(core.os, "replace", side_effect=locked_replace), patch.object(core.time, "sleep") as sleep:
                core.write_json(path, {"status": "split"})
            self.assertEqual(core.read_json(path), {"status": "split"})
            self.assertEqual(len(calls), 4)
            self.assertTrue(all(call == calls[0] for call in calls))
            self.assertEqual(sleep.call_count, 3)
            self.assertEqual([p.name for p in path.parent.iterdir()], ["manifest.json"])

    def test_csv_retries_transient_lock(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "final_rows.csv"
            real_replace = os.replace
            with patch.object(core.os, "replace") as replace, patch.object(core.time, "sleep"):
                # The second attempt performs the actual replacement.
                def attempt(source, destination):
                    if replace.call_count == 1:
                        raise windows_error(5)
                    return real_replace(source, destination)
                replace.side_effect = attempt
                core.write_csv(path, [{"event": "Rainfall"}], ("event",))
            self.assertIn("Rainfall", path.read_text(encoding="utf-8"))
            self.assertEqual(replace.call_count, 2)

    def test_permanent_lock_is_bounded_and_cleans_temporary_file(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            core.write_json(path, {"status": "preparation"})
            error = windows_error(5)
            with patch.object(core.os, "replace", side_effect=error) as replace, patch.object(core.time, "sleep") as sleep:
                with self.assertRaises(PermissionError) as caught:
                    core.write_json(path, {"status": "split"})
            self.assertIs(caught.exception, error)
            self.assertEqual(replace.call_count, 12)
            self.assertLess(sum(call.args[0] for call in sleep.call_args_list), 5)
            self.assertEqual(core.read_json(path), {"status": "preparation"})
            self.assertEqual([p.name for p in path.parent.iterdir()], ["manifest.json"])

    def test_unrelated_errors_fail_without_retry(self):
        for error in (PermissionError(errno.EACCES, "Denied"), windows_error(87), OSError(errno.ENOSPC, "Disk full")):
            with self.subTest(error=error), TemporaryDirectory() as directory:
                path = Path(directory) / "manifest.json"
                with patch.object(core.os, "replace", side_effect=error) as replace, patch.object(core.time, "sleep") as sleep:
                    with self.assertRaises(OSError) as caught:
                        core.write_json(path, {})
                self.assertIs(caught.exception, error)
                replace.assert_called_once()
                sleep.assert_not_called()
                self.assertEqual(list(path.parent.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
