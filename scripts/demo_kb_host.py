"""Exercise a temporary project through the Windows preview's authenticated gateway."""
import json
from contextlib import ExitStack
import unittest

import kb_host_bridge as bridge
from mcp_gateway import GatewayError
from test_kb_authority import GateTests
from test_kb_host_bridge import BridgeTests
from test_service_api import ServiceAPITests


def main():
    fixture = BridgeTests()
    with ExitStack() as cleanup:
        cleanup.callback(fixture.doCleanups)
        fixture.setUp()
        project, = fixture.cfg["projects"]
        body = fixture.f.f.rows("SELECT body FROM v2_task WHERE task_id='T-A'")[0]["body"]
        revision = json.loads(body)["kb_task"]["kb_revision"]
        def read():
            return fixture.call("kb.pull", record_id="S1")

        record = read()
        fixture.assertEqual(record["quote"], "Named evidence")
        print(f"Task T-A | Project {project} | Revision {revision} | {record['id']}: {record['quote']}")
        fixture.policy["actors"][fixture.actor]["read"] = []
        fixture.save()
        with fixture.assertRaises(GatewayError):
            read()
        print("Revoked task read denied")
        fixture.policy["actors"][fixture.actor]["read"] = [project]
        fixture.save()
        fixture.assertEqual(read(), record)
        previous = bridge.deployment(fixture.f.state)
        fixture.cfg["enabled"] = False
        fixture.save()
        with fixture.assertRaises(bridge.APIError) as denied:
            bridge.call(fixture.f.state, fixture.f.root, fixture.token, "kb.pull",
                        {"task_id": "T-A", "record_id": "S1"}, previous)
        fixture.assertEqual(denied.exception.code, "forbidden_role")
        print("Disabled plugin refused previous handle")

    cases = unittest.TestSuite([
        GateTests("test_due_date_rollover_preserves_signed_review_and_rechecks_grants"),
        ServiceAPITests("test_all_lifecycle_tools_preserve_independent_review_and_closure"),
    ])
    if not unittest.TextTestRunner().run(cases).wasSuccessful():
        return 1
    print("Signed review survived date rollover. Independent ordinary task accepted.")
    print("AWO KB HOST PREVIEW PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
