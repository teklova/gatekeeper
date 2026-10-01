import os
import tempfile
import unittest

from gatekeeper.config_loader import get_config


class TestConfigLoader(unittest.TestCase):
    def test_environment_overrides_config_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.json")
            with open(config_path, "w", encoding="utf-8") as handle:
                handle.write('{"camera_source": "config-stream", "db_path": "config.db", "max_dwell_threshold_seconds": 600}')

            old_values = {
                "TENANT_ID": os.environ.get("TENANT_ID"),
                "CLIENT_ID": os.environ.get("CLIENT_ID"),
                "SITE_ID": os.environ.get("SITE_ID"),
                "RTSP_STREAM_URL": os.environ.get("RTSP_STREAM_URL"),
                "DB_PATH": os.environ.get("DB_PATH"),
                "MIN_TRACK_FRAMES": os.environ.get("MIN_TRACK_FRAMES"),
                "MIN_DWELL_SECONDS": os.environ.get("MIN_DWELL_SECONDS"),
                "MAX_DWELL_SECONDS": os.environ.get("MAX_DWELL_SECONDS"),
                "HEALTH_PORT": os.environ.get("HEALTH_PORT"),
                "SYNC_WARNING_THRESHOLD_SECONDS": os.environ.get(
                    "SYNC_WARNING_THRESHOLD_SECONDS"
                ),
                "CLOUD_CHECKIN_INTERVAL_SECONDS": os.environ.get(
                    "CLOUD_CHECKIN_INTERVAL_SECONDS"
                ),
                "AT_SANDBOX_MODE": os.environ.get("AT_SANDBOX_MODE"),
                "AT_SANDBOX_API_KEY": os.environ.get("AT_SANDBOX_API_KEY"),
            }

            try:
                os.environ["TENANT_ID"] = "tenant-env"
                os.environ["SITE_ID"] = "site-env"
                os.environ["RTSP_STREAM_URL"] = "env-stream"
                os.environ["DB_PATH"] = "env.db"
                os.environ["MIN_TRACK_FRAMES"] = "7"
                os.environ["MIN_DWELL_SECONDS"] = "4"
                os.environ["MAX_DWELL_SECONDS"] = "900"
                os.environ["HEALTH_PORT"] = "9090"
                os.environ["SYNC_WARNING_THRESHOLD_SECONDS"] = "240"
                os.environ["CLOUD_CHECKIN_INTERVAL_SECONDS"] = "120"
                os.environ["AT_SANDBOX_MODE"] = "true"
                os.environ["AT_SANDBOX_API_KEY"] = "sandbox-test-key"

                config = get_config(config_path)
                self.assertEqual(config["tenant_id"], "tenant-env")
                self.assertEqual(config["client_id"], "tenant-env")
                self.assertEqual(config["site_id"], "site-env")
                self.assertEqual(config["camera_source"], "env-stream")
                self.assertEqual(config["db_path"], "env.db")
                self.assertEqual(config["min_track_frames"], 7)
                self.assertEqual(config["min_dwell_threshold_seconds"], 4)
                self.assertEqual(config["max_dwell_threshold_seconds"], 900)
                self.assertEqual(config["health_port"], 9090)
                self.assertEqual(config["sync_warning_threshold_seconds"], 240)
                self.assertEqual(config["cloud_checkin_interval_seconds"], 120)
                self.assertTrue(config["sandbox_mode"])
                self.assertEqual(config["sandbox_api_key"], "sandbox-test-key")
            finally:
                for key, value in old_values.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


if __name__ == "__main__":
    unittest.main()

