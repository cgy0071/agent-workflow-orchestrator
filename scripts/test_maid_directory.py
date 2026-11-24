"""Project selection must reject ambiguities before pulling material or preparing a task."""
import json
from pathlib import Path
import tempfile
import unittest

from maid_routing import select


class DirectoryBoundaryTests(unittest.TestCase):
    def test_project_ownership_and_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            student, materials = base / "student", base / "materials"
            student.mkdir()
            materials.mkdir()
            directory = base / "directory.json"
            data = {"version": 1, "projects": {
                "student": {"manager": "student-lead", "root": str(student)},
                "materials": {"manager": "materials-lead", "root": str(materials)}}}
            directory.write_text(json.dumps(data), encoding="utf-8")
            for source, thread in ((materials, base / "thread"), (student, materials / "thread")):
                with self.subTest(source=source, thread=thread), self.assertRaises(ValueError):
                    select(directory, "student", None, {"repository": str(source)}, thread)
            route, body = select(directory, "student", None, {"repository": str(student)}, base / "thread")
            self.assertEqual(route, {"project": "student", "manager": "student-lead"})
            self.assertIsNone(body)
