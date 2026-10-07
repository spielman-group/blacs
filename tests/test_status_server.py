"""Behavioural tests for the status BLACS serves to a remote runmanager.

A real BlacsServer runs on a free port and is reached through BlacsClient, as
runmanager's status poll reaches it.
"""
import unittest

# fixtures does the guarded import of BLACS, once, for every test module.
from fixtures import BlacsServer
import blacs.__main__
from blacs.client import BlacsClient


class FakeShotExecutor(object):
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.requesting_shots = False
        self.local_error = 'Aborted'

    def get_status_snapshot(self):
        return dict(self.snapshot)


class FakeBLACS(object):
    def __init__(self, snapshot):
        self.shot_executor = FakeShotExecutor(snapshot)


SNAPSHOT = {
    'requesting_shots': True,
    'status': 'Running (program time: 0.100s)...',
    'shot_id': 'shot-1',
    'shot_path': '/tmp/shot_a.h5',
    'error': None,
}


class StatusServerTests(unittest.TestCase):
    def setUp(self):
        self.blacs = FakeBLACS(SNAPSHOT)
        self.real_app = getattr(blacs.__main__, 'app', None)
        blacs.__main__.app = self.blacs
        self.server = BlacsServer(bind_address='tcp://127.0.0.1')
        self.addCleanup(self.server.shutdown)
        self.client = BlacsClient(host='127.0.0.1', port=self.server.port, timeout=5)

    def tearDown(self):
        if self.real_app is None:
            del blacs.__main__.app
        else:
            blacs.__main__.app = self.real_app

    def test_a_status_request_gets_what_blacs_is_doing(self):
        self.assertEqual(self.client.get_status(), SNAPSHOT)

    # That the server offers nothing which changes BLACS is the boundary rule
    # rather than a fact about this server, so it is enforced in
    # test_architecture.py alongside the other half of it.


if __name__ == '__main__':
    unittest.main()
