import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import secure_store


@unittest.skipUnless(os.name == "nt", "Windows DPAPI only")
class SecureStoreTests(unittest.TestCase):
    def test_round_trip_uses_encrypted_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "key.bin"
            with patch.object(secure_store, "KEY_FILE", path):
                secure_store.save_key("dummy-secret-for-test")
                self.assertNotIn(b"dummy-secret-for-test", path.read_bytes())
                self.assertEqual(secure_store.load_key(), "dummy-secret-for-test")
                secure_store.remove_key()
                self.assertFalse(path.exists())
