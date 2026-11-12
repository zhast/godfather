"""A dependent must see one logical generation throughout parent startup."""
import os
import sys
import unittest
from unittest import mock

import test_router as fixture
import godfather_lifecycle as lifecycle


class RegistrationTests(unittest.TestCase):
    setUp = fixture.RouterTests.setUp

    def test_first_segment_publication_already_belongs_to_router_generation(self):
        base = self.root / 'runs/parent'
        base.parent.mkdir()
        observed = []
        real_write = fixture.a.write
        with mock.patch.dict(os.environ, dict(self.env, AGENT_ROUTER_NAME='parent',
                                             AGENT_ROUTER_ID='logical-generation'), clear=True):
            real_write(str(base) + '.route.json', dict(run_id='logical-generation',
                       status='running', started_at=fixture.a.now()))

            def observe_write(path, run):
                real_write(path, run)
                if str(path).endswith('.run.json'):
                    # Reproduce a queue-daemon read at each publication boundary.
                    observed.append(lifecycle.dependency_instance('parent')[0])

            argv = ['agent_oversight.py', 'begin', str(base), 'codex', 'fake', str(self.prompt)]
            with mock.patch.object(sys, 'argv', argv), mock.patch.object(fixture.a, 'write', side_effect=observe_write):
                self.assertEqual(0, fixture.a.internal_main())
        self.assertTrue(observed)
        self.assertEqual(['logical-generation'] * len(observed), observed)
