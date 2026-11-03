"""Small authenticated resumable-upload rendering API (standard library only)."""
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import threading
import uuid
from urllib.parse import unquote, urlsplit
from .engine import ManifestError, FILENAME, command_for, validate_manifest

ROOT = Path(os.environ.get('RENDERER_DATA_DIR', '/data/jobs')).resolve()
TOKEN = os.environ.get('RENDERER_API_TOKEN', '')
PORT = int(os.environ.get('PORT', '8080'))
GPU = os.environ.get('RENDERER_ENCODER', 'nvenc') == 'nvenc'
MAX_BODY = 1024 * 1024
LOCK = threading.RLock()
TASKS = queue.Queue(maxsize=100)
ACTIVE = {}
JOB_ID = re.compile(r'^[a-f0-9-]{36}$')


def load(jid):
    file = ROOT / jid / 'job.json'
    if not file.is_file():
        return None
    return json.loads(file.read_text(encoding='utf-8'))


def save(job):
    path = ROOT / job['id'] / 'job.json'
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(job, sort_keys=True), encoding='utf-8')
    os.replace(temp, path)


def safe_path(jid, name):
    if not JOB_ID.fullmatch(jid):
        return None
    if name and (not FILENAME.fullmatch(name) or name in ('.', '..')):
        return None
    return ROOT / jid / 'assets' / name if name else ROOT / jid


def boot_recover():
    ROOT.mkdir(parents=True, exist_ok=True)
    for path in ROOT.glob('*/job.json'):
        try:
            job = json.loads(path.read_text(encoding='utf-8'))
            if job['state'] in ('running', 'queued'):
                job['state'] = 'failed'
                job['error'] = 'Worker restarted/interrupted; client may resubmit this job'
                save(job)
        except (OSError, ValueError, KeyError):
            pass


def worker():
    while True:
        jid = TASKS.get()
        try:
            with LOCK:
                job = load(jid)
                if job is None or job['state'] != 'queued':
                    continue
                job['state'] = 'running'
                save(job)
            root = ROOT / jid
            logfile = root / 'render.log'
            commands = command_for(job['manifest'], root, gpu=GPU)
            failure = None
            for argv in commands:
                with logfile.open('ab') as log:
                    proc = subprocess.Popen(argv, cwd=root, stdout=log, stderr=subprocess.STDOUT,
                                            start_new_session=True)
                    with LOCK:
                        ACTIVE[jid] = proc
                    code = proc.wait()
                    with LOCK:
                        ACTIVE.pop(jid, None)
                        current = load(jid)
                        canceled = current is not None and current['state'] == 'canceled'
                    if canceled:
                        failure = 'canceled'
                        break
                    if code != 0:
                        failure = f'Render stage exited with code {code}; inspect /log'
                        break
            with LOCK:
                current = load(jid)
                if current and current['state'] != 'canceled':
                    out = root / 'output.mp4'
                    if failure or not out.exists() or out.stat().st_size <= 0:
                        current['state'] = 'failed'
                        current['error'] = failure or 'Renderer produced no output'
                    else:
                        current['state'] = 'completed'
                        current['output_bytes'] = out.stat().st_size
                        current['output_sha256'] = file_hash(out)
                    save(current)
        except Exception as exc:
            with LOCK:
                current = load(jid)
                if current and current['state'] != 'canceled':
                    current['state'] = 'failed'
                    current['error'] = f'{type(exc).__name__}: renderer failed'
                    save(current)
        finally:
            with LOCK:
                ACTIVE.pop(jid, None)
            TASKS.task_done()


def file_hash(path):
    h = sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        # Do not log Authorization or URL query tokens (query tokens are not supported).
        print('%s %s' % (self.address_string(), fmt % args), flush=True)

    def reply(self, status, obj, extra=None):
        data = json.dumps(obj, separators=(',', ':')).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        if extra:
            for k, v in extra.items():
                self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(data)

    def authed(self):
        if not TOKEN or not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + TOKEN):
            self.close_connection = True  # Reject credentials before reading body.
            self.reply(401, {'error': 'unauthorized'})
            return False
        return True

    def parts(self):
        path = urlsplit(self.path).path
        try:
            decoded = unquote(path)
            if '%' in decoded or '\\' in decoded or '//' in decoded:
                return []
            return decoded.strip('/').split('/')
        except ValueError:
            return []

    def body(self):
        try:
            size = int(self.headers.get('Content-Length', '-1'))
        except ValueError:
            return None
        if size < 0 or size > MAX_BODY:
            return None
        return self.rfile.read(size)

    def do_GET(self):
        parts = self.parts()
        if parts == ['health', 'live']:
            return self.reply(200, {'status': 'live'})
        if parts == ['health', 'ready']:
            binaries = ('ffmpeg', 'ffprobe', 'editly', 'xvfb-run')
            ready = all(shutil.which(b) for b in binaries) if os.environ.get('RENDERER_STRICT_READY') == '1' else True
            return self.reply(200 if ready else 503, {'status': 'ready' if ready else 'unavailable'})
        if not self.authed():
            return
        if parts == ['v1', 'capabilities']:
            return self.reply(200, {'schema': 'renderer.v1', 'engines': ['editly', 'ffmpeg'],
                                    'output': ['mp4/h264'], 'encoder': 'nvenc' if GPU else 'libx264',
                                    'concurrency': 1, 'native_concat_project_import': False})
        if len(parts) == 3 and parts[:2] == ['v1', 'jobs'] and safe_path(parts[2], None):
            with LOCK:
                job = load(parts[2])
            if not job:
                return self.reply(404, {'error': 'not_found'})
            return self.reply(200, {k: v for k, v in job.items() if k != 'manifest'} | {'manifest': job['manifest']})
        if len(parts) == 4 and parts[:2] == ['v1', 'jobs'] and parts[3] == 'log' and safe_path(parts[2], None):
            log = ROOT / parts[2] / 'render.log'
            if not load(parts[2]):
                return self.reply(404, {'error': 'not_found'})
            return self.stream_file(log, 'text/plain', tail=True)
        if len(parts) == 4 and parts[:2] == ['v1', 'jobs'] and parts[3] == 'output' and safe_path(parts[2], None):
            job = load(parts[2])
            if not job or job['state'] != 'completed':
                return self.reply(409, {'error': 'output_not_ready'})
            return self.stream_file(ROOT / parts[2] / 'output.mp4', 'video/mp4')
        self.reply(404, {'error': 'not_found'})

    def stream_file(self, path, mime, tail=False):
        if not path.is_file():
            return self.reply(404, {'error': 'not_found'})
        size = path.stat().st_size
        start, end = (max(0, size - 65536), size - 1) if tail else (0, size - 1)
        requested = self.headers.get('Range')
        if requested and not tail:
            match = re.fullmatch(r'bytes=(\d+)-(\d*)', requested)
            if not match:
                return self.reply(416, {'error': 'invalid_range'})
            start = int(match[1]); end = min(int(match[2]) if match[2] else size - 1, size - 1)
            if start >= size or start > end:
                return self.reply(416, {'error': 'out_of_range'})
        count = max(0, end - start + 1)
        self.send_response(206 if requested else 200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(count))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Cache-Control', 'no-store')
        if requested:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.end_headers()
        with path.open('rb') as f:
            f.seek(start)
            while count:
                chunk = f.read(min(count, 1024 * 1024))
                if not chunk:
                    break
                self.wfile.write(chunk)
                count -= len(chunk)

    def do_HEAD(self):
        parts = self.parts()
        if not self.authed():
            return
        if len(parts) == 5 and parts[:2] == ['v1', 'jobs'] and parts[3] == 'assets' and safe_path(parts[2], parts[4]):
            job = load(parts[2])
            if not job or parts[4] not in {a['name'] for a in job['manifest']['assets']}:
                return self.reply(404, {'error': 'not_found'})
            base = ROOT / parts[2] / 'assets' / parts[4]
            done = base.is_file()
            offset = base.stat().st_size if done else base.with_name(base.name + '.part').stat().st_size if base.with_name(base.name + '.part').exists() else 0
            self.send_response(200)
            self.send_header('Upload-Offset', str(offset))
            self.send_header('Upload-Complete', 'true' if done else 'false')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        self.reply(404, {'error': 'not_found'})

    def do_POST(self):
        parts = self.parts()
        if not self.authed():
            return
        if parts == ['v1', 'jobs']:
            raw = self.body()
            if raw is None:
                return self.reply(413, {'error': 'body_too_large_or_missing_length'})
            try:
                manifest = validate_manifest(json.loads(raw))
            except (ValueError, UnicodeDecodeError) as exc:
                return self.reply(422, {'error': str(exc)})
            with LOCK:
                jid = str(uuid.uuid4())
                root = ROOT / jid
                (root / 'assets').mkdir(parents=True, exist_ok=False)
                job = {'id': jid, 'state': 'uploading', 'manifest': manifest, 'error': None}
                save(job)
            return self.reply(201, {'id': jid, 'state': 'uploading'})
        if len(parts) == 4 and parts[:2] == ['v1', 'jobs'] and parts[3] in ('start', 'cancel') and safe_path(parts[2], None):
            jid = parts[2]
            with LOCK:
                job = load(jid)
                if not job:
                    return self.reply(404, {'error': 'not_found'})
                if parts[3] == 'cancel':
                    if job['state'] in ('completed', 'failed', 'canceled'):
                        return self.reply(200, {'state': job['state']})
                    job['state'] = 'canceled'
                    save(job)
                    proc = ACTIVE.get(jid)
                    if proc is not None:
                        try:
                            os.killpg(proc.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                    return self.reply(200, {'state': 'canceled'})
                if job['state'] == 'queued' or job['state'] == 'running':
                    return self.reply(200, {'state': job['state']})
                if job['state'] != 'uploading':
                    return self.reply(409, {'error': 'not_startable', 'state': job['state']})
                for asset in job['manifest']['assets']:
                    if not (ROOT / jid / 'assets' / asset['name']).is_file():
                        return self.reply(409, {'error': 'missing_asset', 'name': asset['name']})
                if TASKS.full():
                    return self.reply(503, {'error': 'queue_full'})
                job['state'] = 'queued'
                save(job)
                TASKS.put_nowait(jid)
                return self.reply(202, {'state': 'queued'})
        self.reply(404, {'error': 'not_found'})

    def do_PUT(self):
        parts = self.parts()
        if not self.authed():
            return
        if len(parts) != 5 or parts[:2] != ['v1', 'jobs'] or parts[3] != 'assets' or not safe_path(parts[2], parts[4]):
            return self.reply(404, {'error': 'not_found'})
        jid, name = parts[2], parts[4]
        with LOCK:
            job = load(jid)
            if job is None:
                return self.reply(404, {'error': 'not_found'})
            if job['state'] != 'uploading':
                return self.reply(409, {'error': 'not_uploadable'})
            matching = [a for a in job['manifest']['assets'] if a['name'] == name]
            if not matching:
                return self.reply(404, {'error': 'asset_not_in_manifest'})
            asset = matching[0]
            base = ROOT / jid / 'assets' / name
            part = base.with_name(base.name + '.part')
            try:
                length = int(self.headers.get('Content-Length', '-1'))
                offset = int(self.headers.get('Upload-Offset', '-1'))
            except ValueError:
                return self.reply(400, {'error': 'invalid_upload_headers'})
            actual = base.stat().st_size if base.exists() else part.stat().st_size if part.exists() else 0
            if base.exists():
                return self.reply(200, {'complete': True, 'offset': actual})
            if length <= 0 or length > 16 * 1024 * 1024 or offset != actual or actual + length > asset['size_bytes']:
                return self.reply(409, {'error': 'invalid_chunk_or_offset', 'offset': actual, 'max_chunk': 16 * 1024 * 1024})
            with part.open('ab') as out:
                remain = length
                while remain:
                    block = self.rfile.read(min(remain, 1024 * 1024))
                    if not block:
                        return self.reply(400, {'error': 'incomplete_upload'})
                    out.write(block)
                    remain -= len(block)
            offset += length
            if offset == asset['size_bytes']:
                if file_hash(part) != asset['sha256']:
                    part.unlink(missing_ok=True)
                    return self.reply(422, {'error': 'sha256_mismatch', 'offset': 0})
                part.rename(base)
                return self.reply(200, {'complete': True, 'offset': offset})
            return self.reply(200, {'complete': False, 'offset': offset})


def main():
    if len(TOKEN) < 32:
        raise SystemExit('RENDERER_API_TOKEN must be at least 32 characters; refuse insecure start')
    boot_recover()
    threading.Thread(target=worker, daemon=True, name='render-worker').start()
    server = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    print(f'Cloud renderer listening on :{PORT}; encoder={"NVENC" if GPU else "libx264"}', flush=True)
    server.serve_forever()

if __name__ == '__main__':
    main()
