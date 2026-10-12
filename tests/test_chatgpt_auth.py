import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from chatgpt_auth import ChatGPTAuthError, ChatGPTSession, _validate_id_token


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class AuthTests(unittest.TestCase):
    def test_verifies_signed_identity_and_nonce(self):
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = private.public_key().public_numbers()
        header = {"alg": "RS256", "kid": "test-key"}
        claims = {
            "iss": "https://auth.openai.com", "aud": "oaiapp_test", "nonce": "fresh-nonce",
            "sub": "user-123", "exp": time.time() + 3600,
        }
        message = b64(json.dumps(header).encode()) + "." + b64(json.dumps(claims).encode())
        signature = private.sign(message.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        token = message + "." + b64(signature)
        jwks = {"keys": [{
            "kid": "test-key", "kty": "RSA", "use": "sig",
            "n": b64(public.n.to_bytes((public.n.bit_length() + 7) // 8, "big")),
            "e": b64(public.e.to_bytes((public.e.bit_length() + 7) // 8, "big")),
        }]}

        def fake_get(url):
            if url.endswith("openid-configuration"):
                return {"issuer": "https://auth.openai.com", "jwks_uri": "https://auth.openai.com/.well-known/jwks.json"}
            return jwks

        with patch("chatgpt_auth._json_request", side_effect=fake_get):
            self.assertEqual(_validate_id_token(token, "oaiapp_test", "fresh-nonce")["sub"], "user-123")
            with self.assertRaises(ChatGPTAuthError):
                _validate_id_token(token, "oaiapp_test", "wrong-nonce")

    def test_refresh_replaces_and_protects_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.bin"
            session = ChatGPTSession(path)
            session.data["accounts"]["oaiapp_test"] = {
                "client_id": "oaiapp_test", "subject": "user-123", "email": "test@example.com",
                "access_token": "old-access", "refresh_token": "old-refresh", "expires_at": 0,
            }
            session.data["active_id"] = "oaiapp_test"
            session._save()
            self.assertNotIn(b"old-refresh", path.read_bytes())
            with patch("chatgpt_auth._json_request", return_value={
                "access_token": "new-access", "refresh_token": "new-refresh", "scope": "chatgpt.tokens.use.direct",
                "expires_in": 3600,
            }):
                self.assertEqual(session.access_token(), "new-access")
            self.assertEqual(ChatGPTSession(path).access_token(), "new-access")

    def test_dynamic_registration_saves_plan_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            session = ChatGPTSession(Path(directory) / "auth.bin")
            callback = {}
            opened = []

            class FakeServer:
                server_port = 1455

                def handle_request(self):
                    callback.update({"code": "auth-code", "client_id": "oaiapp_test", "error": ""})

                def server_close(self):
                    pass

            with patch("chatgpt_auth._listen_for_code", return_value=(FakeServer(), callback)), \
                 patch("chatgpt_auth.webbrowser.open", side_effect=lambda url: opened.append(url) or True), \
                 patch("chatgpt_auth._json_request", return_value={
                     "access_token": "access", "refresh_token": "refresh", "id_token": "signed-id",
                     "scope": "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct",
                     "expires_in": 3600,
                 }), \
                 patch("chatgpt_auth._validate_id_token", return_value={"sub": "user-123", "email": "test@example.com"}):
                label = session.sign_in()
            self.assertIn("test@example.com", label)
            query = parse_qs(urlsplit(opened[0]).query)
            self.assertEqual(query["client_id"], ["dynamic_agent_client"])
            self.assertIn("chatgpt.tokens.use.direct", query["scope"][0])
            self.assertEqual(ChatGPTSession(session.auth_file).access_token(), "access")


if __name__ == "__main__":
    unittest.main()
