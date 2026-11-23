import contextlib
import io
import json
import subprocess
import unittest
from unittest.mock import patch

import agy_turn
import awo_project


class AgyTests(unittest.TestCase):
    def invoke(self, result):
        buf = io.StringIO()
        with patch.object(agy_turn, "binary", return_value="agy"), patch.object(
            subprocess, "run", side_effect=result
        ), contextlib.redirect_stdout(buf):
            code = awo_project.main(["ask", "agy", "--model", "gemini-demo", "--prompt", "hello"])
        return code, json.loads(buf.getvalue())

    def test_request_model_is_not_inferred_from_success(self):
        code, reply = self.invoke([subprocess.CompletedProcess([], 0, '{"status":"SUCCESS","response":"Hi"}')])
        self.assertEqual(code, 0)
        self.assertEqual(reply["requested_model"], "gemini-demo")
        self.assertIsNone(reply["reported_model"])

    def test_failed_or_incomplete_turn(self):
        for result in (subprocess.TimeoutExpired("agy", 55), subprocess.CompletedProcess([], 1, ""),
                       subprocess.CompletedProcess([], 0, "{}"), subprocess.CompletedProcess([], 0, "bad")):
            with self.subTest(result=result):
                code, reply = self.invoke([result])
                self.assertEqual(code, 1)
                self.assertEqual(reply["status"], "failed")
