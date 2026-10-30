import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aghub import keys  # noqa: E402
from aghub.board import ensure_local_agent_key, local_agent_key  # noqa: E402
from aghub.errors import AghubError  # noqa: E402


class Ed25519Tests(unittest.TestCase):
    def test_rfc8032_empty_message_vector(self):
        seed = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
        public = keys.public_key(bytes.fromhex(seed)).hex()
        self.assertEqual(
            public,
            "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        )
        signature = keys.sign(seed, b"")
        self.assertEqual(
            signature,
            "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
            "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
        )
        self.assertTrue(keys.verify(public, b"", signature))

    def test_rfc8032_one_byte_message_vector(self):
        seed = "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb"
        public = keys.public_key(bytes.fromhex(seed)).hex()
        signature = keys.sign(seed, bytes([0x72]))
        self.assertEqual(
            public,
            "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        )
        self.assertTrue(keys.verify(public, bytes([0x72]), signature))
        self.assertFalse(keys.verify(public, bytes([0x73]), signature))

    def test_malformed_signatures_fail_closed(self):
        _, public = keys.generate()
        self.assertFalse(keys.verify(public, b"message", "00"))
        self.assertFalse(keys.verify("not-hex", b"message", "00" * 64))
        self.assertFalse(keys.verify(public, b"message", "00" * 64))


class EnvironmentKeyTests(unittest.TestCase):
    seed = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aghub-key-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(os.environ.pop, "AGHUB_AGENT_KEY", None)
        os.environ.pop("AGHUB_AGENT_KEY", None)

    def test_environment_seed_keeps_a_stable_identity(self):
        os.environ["AGHUB_AGENT_KEY"] = self.seed
        entry = ensure_local_agent_key(self.tmp, "ci")
        self.assertEqual(
            entry["public_key"],
            "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        )
        self.assertEqual(local_agent_key(self.tmp, "ci")["private_key"], self.seed)
        # An explicitly configured key replaces a cached local one.
        os.environ["AGHUB_AGENT_KEY"] = "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb"
        entry = ensure_local_agent_key(self.tmp, "ci")
        self.assertNotEqual(entry["public_key"], self.seed)

    def test_malformed_environment_key_is_rejected(self):
        os.environ["AGHUB_AGENT_KEY"] = "not-hex"
        with self.assertRaises(AghubError):
            ensure_local_agent_key(self.tmp, "ci")
        os.environ["AGHUB_AGENT_KEY"] = self.seed
        with self.assertRaises(AghubError):
            ensure_local_agent_key(self.tmp, "ci", rotate=True)


if __name__ == "__main__":
    unittest.main()
