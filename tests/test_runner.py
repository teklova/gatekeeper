import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from gatekeeper.runner import GatekeeperRunner


class TestRunnerLifecycle(unittest.TestCase):
    def test_runner_aborts_startup_when_cloud_authentication_fails(self):
        runner = GatekeeperRunner.__new__(GatekeeperRunner)
        runner.setup = Mock()
        runner.stop = Mock()
        runner.sync_worker = SimpleNamespace(
            _remote_backend_enabled=lambda: True,
            authenticate=Mock(side_effect=PermissionError("hardware token rejected")),
        )

        with self.assertRaises(PermissionError):
            runner.start()

        runner.stop.assert_called_once_with()

    def test_runner_aborts_startup_when_cloud_endpoint_is_unconfigured(self):
        runner = GatekeeperRunner.__new__(GatekeeperRunner)
        runner.setup = Mock()
        runner.stop = Mock()
        runner.sync_worker = SimpleNamespace(
            _remote_backend_enabled=lambda: False,
            authenticate=Mock(),
        )

        with self.assertRaises(PermissionError):
            runner.start()

        runner.sync_worker.authenticate.assert_not_called()
        runner.stop.assert_called_once_with()

    def test_runner_initializes_components(self):
        runner = GatekeeperRunner(config_path="config.json")
        runner.setup()

        self.assertIsNotNone(runner.notifier)
        self.assertIsNotNone(runner.tracker)
        self.assertIsNotNone(runner.sync_worker)
        self.assertIsNotNone(runner.health_server)
        runner.stop()

    def test_runner_stop_cleans_up_threads(self):
        runner = GatekeeperRunner(config_path="config.json")
        runner.setup()
        runner.sync_worker.start()
        time.sleep(0.1)
        runner.stop()

        self.assertFalse(runner._running)
        self.assertIsNotNone(runner.sync_worker)
        if runner.sync_worker._thread is not None:
            self.assertFalse(runner.sync_worker._thread.is_alive())

    def test_runner_stop_orders_component_shutdown(self):
        runner = GatekeeperRunner(config_path="config.json")
        calls = []

        runner._running = True
        runner.tracker = SimpleNamespace(stop=lambda: calls.append("tracker"))
        runner.notifier = SimpleNamespace(shutdown=lambda: calls.append("notifier"))
        runner.health_server = SimpleNamespace(
            shutdown=lambda: calls.append("health_shutdown"),
            server_close=lambda: calls.append("health_close"),
        )
        runner.sync_worker = SimpleNamespace(stop=lambda: calls.append("sync"))

        runner.stop()

        self.assertEqual(calls, ["tracker", "notifier", "health_close", "sync"])


if __name__ == "__main__":
    unittest.main()
