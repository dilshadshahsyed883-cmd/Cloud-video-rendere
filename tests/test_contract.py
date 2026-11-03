import hashlib
import http.client
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from renderer.engine import ManifestError, validate_manifest, command_for
from renderer import api

DATA = b'image-bytes-123'
MANIFEST = {
    'engine': 'ffmpeg', 'width': 1280, 'height': 720, 'fps': 30,
    'assets': [{'name': 'a.png', 'size_bytes': len(DATA), 'sha256': hashlib.sha256(DATA).hexdigest()}],
    'clips': [{'kind': 'image', 'asset': 'a.png', 'duration': 2.0}]
}
TOKEN = '0123456789abcdef0123456789abcdef'

class ValidateTest(unittest.TestCase):
    def test_supported(self):
        manifest = validate_manifest(MANIFEST)
        with tempfile.TemporaryDirectory() as d:
            args = command_for(manifest, Path(d), gpu=True)[0]
            self.assertIn('h264_nvenc', args)
            self.assertIn('concat=n=1:v=1:a=0', ' '.join(args))
    def test_bad_paths_rejected(self):
        for name in ('../secret', '/etc/passwd', 'x\\path', '..', 'hello there'):
            m = json.loads(json.dumps(MANIFEST))
            m['assets'][0]['name'] = name
            with self.subTest(name=name), self.assertRaises(ManifestError):
                validate_manifest(m)
    def test_unknown_and_remote_media_rejected(self):
        m = json.loads(json.dumps(MANIFEST))
        m['clips'][0]['asset'] = 'https://example.org/x.png'
        with self.assertRaises(ManifestError):
            validate_manifest(m)
    def test_bad_dimensions_and_duration_rejected(self):
        for mutation in ({'fps': 0}, {'width': 99999}, {'height': 101}):
            with self.subTest(mutation=mutation), self.assertRaises(ManifestError):
                validate_manifest({**MANIFEST, **mutation})
        m = json.loads(json.dumps(MANIFEST))
        m['clips'][0]['duration'] = -1
        with self.assertRaises(ManifestError):
            validate_manifest(m)
    def test_editly_spec_local_only(self):
        m = validate_manifest({**MANIFEST, 'engine': 'editly'})
        with tempfile.TemporaryDirectory() as d:
            argv = command_for(m, Path(d), gpu=True)
            spec = json.loads((Path(d) / 'editly-spec.json').read_text())
            self.assertFalse(spec['allowRemoteRequests'])
            self.assertEqual(argv[0][0], 'xvfb-run')
            self.assertEqual(argv[1][argv[1].index('-c:v')+1], 'h264_nvenc')

class ApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self._original = (api.ROOT, api.TOKEN)
        api.ROOT = Path(self.temp.name)
        api.TOKEN = TOKEN
        api.boot_recover()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), api.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        api.ROOT, api.TOKEN = self._original
        self.temp.cleanup()
    def req(self, method, path, payload=b'', headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        conn.request(method, path, body=payload, headers=headers or {})
        r = conn.getresponse()
        body = r.read()
        out = (r.status, dict(r.getheaders()), json.loads(body) if body else None)
        conn.close()
        return out
    def token_headers(self):
        return {'Authorization': 'Bearer ' + TOKEN}
    def test_auth_create_resume_checksum_and_start(self):
        status, _, _ = self.req('POST', '/v1/jobs', json.dumps(MANIFEST).encode())
        self.assertEqual(status, 401)
        status, _, result = self.req('POST', '/v1/jobs', json.dumps(MANIFEST).encode(), self.token_headers())
        self.assertEqual(status, 201)
        jid = result['id']
        url = f'/v1/jobs/{jid}/assets/a.png'
        half = 5
        status, _, _ = self.req('PUT', url, DATA[:half], {**self.token_headers(), 'Upload-Offset': '0'})
        self.assertEqual(status, 200)
        status, headers, _ = self.req('HEAD', url, b'', self.token_headers())
        self.assertEqual((status, headers.get('Upload-Offset')), (200, str(half)))
        status, _, _ = self.req('POST', f'/v1/jobs/{jid}/start', b'', self.token_headers())
        self.assertEqual(status, 409)
        status, _, _ = self.req('PUT', url, DATA[half:], {**self.token_headers(), 'Upload-Offset': str(half)})
        self.assertEqual(status, 200)
        with patch.object(api.TASKS, 'put_nowait') as put:
            status, _, result = self.req('POST', f'/v1/jobs/{jid}/start', b'', self.token_headers())
            self.assertEqual((status, result['state']), (202, 'queued'))
            put.assert_called_once_with(jid)
    def test_bad_checksum_resets_offset(self):
        status, _, result = self.req('POST', '/v1/jobs', json.dumps(MANIFEST).encode(), self.token_headers())
        url = f'/v1/jobs/{result["id"]}/assets/a.png'
        status, _, body = self.req('PUT', url, b'0'*len(DATA), {**self.token_headers(), 'Upload-Offset': '0'})
        self.assertEqual(status, 422)
        self.assertEqual(body['offset'], 0)
    def test_path_traversal_blocked(self):
        status, _, _ = self.req('GET', '/v1/jobs/%2e%2e/output', b'', self.token_headers())
        self.assertEqual(status, 404)
    def test_missing_token_rejected(self):
        status, _, _ = self.req('GET', '/v1/capabilities')
        self.assertEqual(status, 401)

if __name__ == '__main__':
    unittest.main()
