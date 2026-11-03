"""Reference PC/VPS CLI: submit, resume upload/download, verify MP4 output.

No SaladCloud management key is used. Token is read from the process environment.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlparse
import urllib.error
import urllib.request

CHUNK = 8 * 1024 * 1024

class ApiError(RuntimeError):
    pass


def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for part in iter(lambda: f.read(1024 * 1024), b''):
            h.update(part)
    return h.hexdigest()


class RendererClient:
    def __init__(self, url, token):
        parsed = urlparse(url)
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')):
            raise ValueError('Rendering API requires HTTPS except local loopback tests')
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Do not embed credentials or query parameters in endpoint URLs')
        if not token or len(token) < 32:
            raise ValueError('Renderer bearer token must be at least 32 characters')
        self.url = url.rstrip('/')
        self.token = token

    def request(self, method, path, data=None, headers=None, retries=5):
        req_headers = {'Authorization': 'Bearer ' + self.token}
        req_headers.update(headers or {})
        for attempt in range(retries):
            req = urllib.request.Request(self.url + path, data=data, headers=req_headers, method=method)
            try:
                return urllib.request.urlopen(req, timeout=65)
            except urllib.error.HTTPError as e:
                body = e.read(1000).decode('utf-8', 'replace')
                if e.code not in (429, 502, 503, 504) or attempt == retries - 1:
                    raise ApiError(f'HTTP {e.code} on {method} {path}: {body}') from e
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt == retries - 1:
                    raise ApiError(f'Network error on {method} {path}: {e}') from e
            time.sleep(min(2 ** attempt, 8))

    def json(self, method, path, payload=None):
        if payload is not None:
            data = json.dumps(payload).encode('utf-8')
            headers = {'Content-Type': 'application/json'}
        elif method == 'POST':
            data, headers = b'', {}
        else:
            data, headers = None, {}
        with self.request(method, path, data, headers) as r:
            return json.load(r)

    def upload(self, jid, file, name):
        path = f'/v1/jobs/{jid}/assets/{name}'
        size = file.stat().st_size
        with self.request('HEAD', path) as head:
            offset = int(head.headers.get('Upload-Offset', '0'))
            if offset > size:
                raise ApiError('Server asset offset exceeds local file size')
            if head.headers.get('Upload-Complete') == 'true':
                return
        with file.open('rb') as stream:
            stream.seek(offset)
            while offset < size:
                part = stream.read(CHUNK)
                with self.request('PUT', path, part, {'Upload-Offset': str(offset)}) as res:
                    answer = json.load(res)
                    offset = answer['offset']
                    if offset != stream.tell():
                        raise ApiError('Unexpected server upload offset; abort to avoid file corruption')
        print('Uploaded:', name)

    def download(self, jid, destination, digest):
        temp = destination.with_name(destination.name + '.part')
        offset = temp.stat().st_size if temp.exists() else 0
        headers = {'Range': f'bytes={offset}-'} if offset else {}
        with self.request('GET', f'/v1/jobs/{jid}/output', headers=headers) as response:
            if offset and response.status != 206:
                raise ApiError('Download resume rejected (Range not accepted)')
            if offset and not response.headers.get('Content-Range', '').startswith(f'bytes {offset}-'):
                raise ApiError('Unexpected Content-Range; aborting')
            with temp.open('ab' if offset else 'wb') as out:
                while True:
                    block = response.read(CHUNK)
                    if not block:
                        break
                    out.write(block)
        if file_sha(temp) != digest:
            raise ApiError(f'Output digest mismatch; kept partial file at {temp}')
        temp.replace(destination)
        print(f'Verified complete MP4: {destination} ({destination.stat().st_size:,} bytes)')


def main():
    p = argparse.ArgumentParser(description='Cloud renderer desktop/VPS CLI')
    p.add_argument('--url', required=True, help='SaladCloud HTTPS gateway URL')
    p.add_argument('--manifest', required=True, type=Path)
    p.add_argument('--assets-dir', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--timeout-seconds', type=int, default=7200)
    args = p.parse_args()
    client = RendererClient(args.url, os.environ.get('RENDERER_API_TOKEN', ''))
    manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
    for a in manifest['assets']:
        file = args.assets_dir / a['name']
        if not file.is_file() or not file.resolve().is_relative_to(args.assets_dir.resolve()):
            raise ApiError(f'Missing or unsafe input: {a["name"]}')
        a['sha256'] = file_sha(file)
        a['size_bytes'] = file.stat().st_size
    checkpoint = args.output.with_name(args.output.name + '.job.json')
    project_hash = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode('utf-8')).hexdigest()
    if checkpoint.is_file():
        job = json.loads(checkpoint.read_text())
        jid = job['id'] if job.get('manifest_sha256') == project_hash else None
        if jid is not None:
            try:
                state = client.json('GET', f'/v1/jobs/{jid}')['state']
            except ApiError as e:
                if 'HTTP 404' not in str(e):
                    raise
                jid, state = None, None
        else:
            state = None
    else:
        jid, state = None, None
    if jid is None:
        created = client.json('POST', '/v1/jobs', manifest)
        jid, state = created['id'], created['state']
        checkpoint.write_text(json.dumps({'id': jid, 'manifest_sha256': project_hash}), encoding='utf-8')
    print('Job:', jid, 'state:', state)
    if state == 'uploading':
        for a in manifest['assets']:
            client.upload(jid, args.assets_dir / a['name'], a['name'])
        state = client.json('POST', f'/v1/jobs/{jid}/start')['state']
    deadline = time.monotonic() + args.timeout_seconds
    while state in ('queued', 'running'):
        if time.monotonic() > deadline:
            raise ApiError('Timed out, job retained; restart client to resume checking')
        time.sleep(3)
        details = client.json('GET', f'/v1/jobs/{jid}')
        state = details['state']
        print('Status:', state)
    if state != 'completed':
        raise ApiError('Render did not complete: ' + state)
    details = client.json('GET', f'/v1/jobs/{jid}')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    client.download(jid, args.output, details['output_sha256'])
    checkpoint.unlink(missing_ok=True)

if __name__ == '__main__':
    try:
        main()
    except (ApiError, ValueError, KeyError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        raise SystemExit(1)
