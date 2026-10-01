import importlib.util
import pathlib
import unittest
from unittest.mock import patch


_MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "gatekeeper" / "notifier.py"
_SPEC = importlib.util.spec_from_file_location("sms_service", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
SMSNotifier = _MODULE.SMSNotifier

SMS_SANDBOX_KEY = "sandbox_key_mock"
SMS_RECIPIENT = "+10000000001"


class TestSMSNotifier(unittest.TestCase):
    def test_send_alert_uses_sandbox_web_simulator_contract(self):
        config = {
            "sandbox_mode": True,
            "sandbox_api_key": SMS_SANDBOX_KEY,
            "africastalking": {
                "username": "sandbox",
                "api_key": "production_api_key",
                "recipient_phone": SMS_RECIPIENT,
            },
        }
        notifier = SMSNotifier(config=config)
        self.addCleanup(notifier.shutdown)

        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 201
            mock_post.return_value.text = '{"SMSMessageData":{"Recipients":[{"status":"Success","statusCode":101}]}}'
            mock_post.return_value.json.return_value = {
                "SMSMessageData": {
                    "Message": "Sent to 1/1 Total Cost: KES 0.8000",
                    "Recipients": [{"statusCode": 101, "number": SMS_RECIPIENT, "status": "Success"}],
                }
            }
            with patch("builtins.print") as mock_print:
                result = notifier.send_alert("Test message")

        self.assertTrue(result)
        mock_print.assert_called_once()
        mock_post.assert_called_once_with(
            "https://api.sandbox.africastalking.com/version1/messaging",
            data={"username": "sandbox", "to": SMS_RECIPIENT, "message": "Test message"},
            headers={
                "apiKey": SMS_SANDBOX_KEY,
                "Accept": "application/json",
            },
        )

    def test_send_alert_returns_false_for_rejected_recipient(self):
        notifier = SMSNotifier("sandbox", "YOUR_AFRICASTALKING_API_KEY", SMS_RECIPIENT, sandbox_mode=True, sandbox_api_key=SMS_SANDBOX_KEY)
        self.addCleanup(notifier.shutdown)

        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 201
            mock_post.return_value.text = '{"SMSMessageData":{"Recipients":[{"status":"InvalidPhoneNumber","statusCode":403}]}}'
            mock_post.return_value.json.return_value = {
                "SMSMessageData": {
                    "Message": "Sent to 0/1 Total Cost: KES 0.0000",
                    "Recipients": [
                        {
                            "statusCode": 403,
                            "number": SMS_RECIPIENT,
                            "status": "InvalidPhoneNumber",
                        }
                    ],
                }
            }
            result = notifier.send_alert("Test message")

        self.assertFalse(result)
        mock_post.assert_called_once_with(
            "https://api.sandbox.africastalking.com/version1/messaging",
            data={"username": "sandbox", "to": SMS_RECIPIENT, "message": "Test message"},
            headers={
                "apiKey": SMS_SANDBOX_KEY,
                "Accept": "application/json",
            },
        )

    def test_send_alert_returns_false_when_disabled(self):
        notifier = SMSNotifier("sandbox", "YOUR_AFRICASTALKING_API_KEY", SMS_RECIPIENT, enabled=False)
        self.addCleanup(notifier.shutdown)

        self.assertFalse(notifier.send_alert("Test message"))

    def test_send_alert_uses_sandbox_mock_when_api_key_is_placeholder(self):
        notifier = SMSNotifier("sandbox", "YOUR_AFRICASTALKING_API_KEY", SMS_RECIPIENT)
        self.addCleanup(notifier.shutdown)

        with patch("requests.post") as mock_post:
            result = notifier.send_alert("Test message")

        self.assertTrue(result)
        mock_post.assert_not_called()


if __name__ == "__main__":
    unittest.main()

