"""Handshake com transporte HTTP simulado e timeouts explícitos."""

import base64
import json
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from foto_webrtc.signaling import exchange_local_sdp


class SignalingTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.session.__enter__ = Mock(return_value=self.session)
        self.session.__exit__ = Mock(return_value=False)
        self.auth = SimpleNamespace(
            _probe_tcp_port=Mock(side_effect=lambda ip, port, timeout: port == 8081),
            LocalSignalingPortError=ConnectionError,
            _decrypt_data1_legacy=Mock(return_value="0123456789PUBLIC_KEY0123456789"),
            _decrypt_data1_v3=Mock(return_value="0123456789PUBLIC_KEY0123456789"),
            rsa_load_public_key=Mock(return_value="public-key"),
            _calc_local_path_ending=Mock(return_value="12345"),
            generate_aes_key=Mock(return_value="session-key"),
            aes_encrypt=Mock(return_value="encrypted-offer"),
            rsa_encrypt=Mock(return_value="encrypted-key"),
            aes_decrypt=Mock(return_value="answer"),
        )
        module = ModuleType("unitree_webrtc_connect")
        module.unitree_auth = self.auth
        self.modules = patch.dict(sys.modules, {"unitree_webrtc_connect": module})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.http = patch("requests.Session", return_value=self.session)
        self.http.start()
        self.addCleanup(self.http.stop)

    def test_legacy_offer_disables_proxies_and_bounds_http_wait(self):
        self.session.post.return_value = SimpleNamespace(status_code=200, text="answer", raise_for_status=lambda: None)
        self.assertEqual(exchange_local_sdp("192.168.12.1", "offer", timeout=0.4), "answer")
        self.assertFalse(self.session.trust_env)
        self.session.post.assert_called_once_with("http://192.168.12.1:8081/offer", data="offer",
            headers={"Content-Type": "application/json"}, timeout=(0.4, 0.4))

    def test_new_firmware_keeps_sdk_authentication_and_per_device_key(self):
        for data2 in (2, 3):
            with self.subTest(data2=data2):
                self.auth._probe_tcp_port.side_effect = lambda ip, port, timeout: port == 9991
                notify = base64.b64encode(json.dumps({"data1": "cipher", "data2": data2}).encode()).decode()
                self.session.post.side_effect = [
                    SimpleNamespace(status_code=200, text=notify, raise_for_status=lambda: None),
                    SimpleNamespace(status_code=200, text="encrypted-answer", raise_for_status=lambda: None),
                ]
                self.assertEqual(exchange_local_sdp("192.168.12.1", "offer", "per-device-key"), "answer")
                self.assertEqual(self.session.post.call_args.args[0], "http://192.168.12.1:9991/con_ing_12345")
                self.assertEqual(self.session.post.call_args.kwargs["timeout"], (2.0, 5.0))
        self.auth._decrypt_data1_v3.assert_called_once_with("cipher", "per-device-key")

    def test_unreachable_robot_raises_without_http_request(self):
        self.auth._probe_tcp_port.side_effect = lambda *args, **kwargs: False
        with self.assertRaises(ConnectionError):
            exchange_local_sdp("192.168.12.1", "offer")
        self.session.post.assert_not_called()

    def test_network_timeout_propagates_to_worker(self):
        import requests

        self.session.post.side_effect = requests.Timeout("No SDP answer")
        with self.assertRaises(requests.Timeout):
            exchange_local_sdp("192.168.12.1", "offer")
