import hashlib
from pathlib import Path
import tempfile
import unittest
from clients.render_client import RendererClient, file_sha

class ClientTest(unittest.TestCase):
    def test_file_digest(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'a.txt';p.write_bytes(b'abc')
            self.assertEqual(file_sha(p), hashlib.sha256(b'abc').hexdigest())
    def test_no_plaintext_remote(self):
        with self.assertRaises(ValueError):
            RendererClient('http://salad.example.org', 'a'*32)
    def test_loopback_test_allowed(self):
        self.assertTrue(RendererClient('http://127.0.0.1:8080', 'a'*32).url)
