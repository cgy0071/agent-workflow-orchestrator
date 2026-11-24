import os
import shutil
import subprocess
import unittest

import awo_service as service
import project_domain
import test_service_api as fixtures


class DomainTests(unittest.TestCase):
    def project(self, bind=True):
        f = fixtures.ServiceAPITests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        if bind:
            command = f.f.envelope("project.bind_root", {"project_root": str(f.root), "expected_revision": 0})
            service.execute_command(f.state, *f.f.actor, command)
        return f

    def test_one_client_resolves_two_projects_across_git_commits(self):
        a = self.project()
        b = self.project()
        git = ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
               "-c", "commit.gpgsign=false", "-c", "core.hooksPath=" + os.devnull, "-C", str(a.root)]
        subprocess.run(git + ["init", "-q"], check=True)
        subprocess.run(git + ["commit", "--allow-empty", "-qm", "first"], check=True)
        first = project_domain.lookup(a.state, a.root, a.worker)
        other = project_domain.lookup(b.state, b.root, b.worker)
        self.assertNotEqual(first["project_id"], other["project_id"])
        subprocess.run(git + ["commit", "--allow-empty", "-qm", "second"], check=True)
        self.assertEqual(first, project_domain.lookup(a.state, a.root, a.worker, first["project_id"]))
        for root, token, expected in ((b.root, a.worker, None), (a.root, b.worker, None),
                                      (a.root, a.worker, other["project_id"])):
            with self.assertRaises(service.ServiceError):
                project_domain.lookup(a.state, root, token, expected)

    def test_refuses_unbound_project_scoped_token_and_copied_state(self):
        f = self.project(bind=False)
        with self.assertRaisesRegex(service.ServiceError, "bind"):
            project_domain.lookup(f.state, f.root, f.worker)
        service.execute_command(f.state, *f.f.actor, f.f.envelope(
            "project.bind_root", {"project_root": str(f.root), "expected_revision": 0}))
        f.call(f.controller, "awo_task_create", f.args(task_id="T-SCOPED", title="Scoped task"))
        scoped = f.issue("worker", "executor", task_id="T-SCOPED")
        with self.assertRaisesRegex(service.ServiceError, "Project-wide"):
            project_domain.lookup(f.state, f.root, scoped)
        copied = f.root / "copied-state"
        shutil.copytree(f.state, copied)
        with self.assertRaisesRegex(service.ServiceError, "binding mismatch"):
            project_domain.lookup(copied, f.root, f.worker)
