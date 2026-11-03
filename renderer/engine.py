"""Safely translate the subset of the renderer JSON contract to Editly or FFmpeg.

This is *not* a Concat-native project importer. An adapter from Concat's project
schema is needed for projects containing Concat-specific transitions/keyframes.
"""
import json
import os
from pathlib import Path
import re

FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MAX_CLIPS = 300

class ManifestError(ValueError):
    pass


def validate_manifest(raw):
    if not isinstance(raw, dict):
        raise ManifestError('manifest must be a JSON object')
    engine = raw.get('engine', 'editly')
    if engine not in ('editly', 'ffmpeg'):
        raise ManifestError('engine must be editly or ffmpeg')
    width, height, fps = raw.get('width', 1920), raw.get('height', 1080), raw.get('fps', 30)
    if type(width) is not int or type(height) is not int or not (320 <= width <= 3840 and 240 <= height <= 2160):
        raise ManifestError('invalid dimensions')
    if width % 2 or height % 2:
        raise ManifestError('width and height must be even')
    if type(fps) is not int or not (1 <= fps <= 60):
        raise ManifestError('invalid fps')
    clips = raw.get('clips')
    if not isinstance(clips, list) or not 1 <= len(clips) <= MAX_CLIPS:
        raise ManifestError('clips must contain between 1 and 300 entries')
    assets = raw.get('assets')
    if not isinstance(assets, list) or not 1 <= len(assets) <= 500:
        raise ManifestError('assets must be an array')
    known = {}
    for a in assets:
        if not isinstance(a, dict):
            raise ManifestError('invalid asset entry')
        name, size, digest = a.get('name'), a.get('size_bytes'), a.get('sha256')
        if not isinstance(name, str) or not FILENAME.fullmatch(name) or name in ('.', '..') or name in known:
            raise ManifestError('asset names must be unique safe basenames')
        if type(size) is not int or not (1 <= size <= 2_147_483_648):
            raise ManifestError('invalid asset size (maximum 2 GiB per file)')
        if not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest):
            raise ManifestError('sha256 must be lowercase hex')
        known[name] = {'name': name, 'size_bytes': size, 'sha256': digest}
    if sum(a['size_bytes'] for a in known.values()) > 20 * 1024**3:
        raise ManifestError('project exceeds maximum 20 GiB')
    cleaned_clips = []
    duration_total = 0
    for c in clips:
        if not isinstance(c, dict) or c.get('kind') not in ('image', 'video'):
            raise ManifestError('clip kind must be image or video')
        src = c.get('asset')
        if not isinstance(src, str) or src not in known:
            raise ManifestError('clip asset must be listed in assets')
        duration = c.get('duration')
        if type(duration) not in (float, int) or not 0.1 <= duration <= 600:
            raise ManifestError('clip duration must be 0.1..600 seconds')
        duration_total += duration
        if duration_total > 36000:
            raise ManifestError('maximum duration is 10 hours')
        v = {'kind': c['kind'], 'asset': src, 'duration': duration}
        if 'title' in c:
            if engine == 'ffmpeg':
                raise ManifestError('title overlays require engine=editly')
            if not isinstance(c['title'], str) or len(c['title']) > 250:
                raise ManifestError('invalid title')
            v['title'] = c['title']
        cleaned_clips.append(v)
    audio = raw.get('audio')
    if audio is not None and (not isinstance(audio, str) or audio not in known):
        raise ManifestError('audio must be a listed asset')
    if raw.get('codec', 'h264') != 'h264':
        raise ManifestError('only h264 is supported')
    return {'engine': engine, 'width': width, 'height': height, 'fps': fps,
            'assets': list(known.values()), 'clips': cleaned_clips,
            'audio': audio, 'codec': 'h264'}


def command_for(manifest, job_dir, gpu=True):
    """Return shell-free argv. Only references uploaded, validated local files."""
    job_dir = Path(job_dir)
    assets = job_dir / 'assets'
    output = job_dir / 'output.mp4'
    tmp = job_dir / 'render-temp.mp4'
    if manifest['engine'] == 'editly':
        edit_spec = {
            'outPath': str(tmp), 'width': manifest['width'], 'height': manifest['height'],
            'fps': manifest['fps'], 'allowRemoteRequests': False, 'keepSourceAudio': False,
            'defaults': {'transition': None},
            'clips': [],
        }
        for clip in manifest['clips']:
            layers = [{'type': clip['kind'], 'path': str(assets / clip['asset'])}]
            if 'title' in clip:
                layers.append({'type': 'title', 'text': clip['title']})
            edit_spec['clips'].append({'duration': clip['duration'], 'layers': layers, 'transition': None})
        if manifest['audio']:
            edit_spec['audioFilePath'] = str(assets / manifest['audio'])
        spec_path = job_dir / 'editly-spec.json'
        spec_path.write_text(json.dumps(edit_spec), encoding='utf-8')
        first = ['xvfb-run', '-a', 'editly', str(spec_path)]
        # GPU encode is a *separate* output pass, not GPU acceleration of all Editly effects.
        second = ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error', '-i', str(tmp),
                  '-c:v', 'h264_nvenc' if gpu else 'libx264', '-pix_fmt', 'yuv420p',
                  '-c:a', 'copy', '-movflags', '+faststart', str(output)]
        return [first, second]
    args = ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error']
    for clip in manifest['clips']:
        if clip['kind'] == 'image':
            args += ['-loop', '1', '-framerate', str(manifest['fps']), '-t', str(clip['duration'])]
        else:
            args += ['-stream_loop', '-1', '-t', str(clip['duration'])]
        args += ['-i', str(assets / clip['asset'])]
    if manifest['audio']:
        args += ['-stream_loop', '-1', '-i', str(assets / manifest['audio'])]
    parts = []
    for i in range(len(manifest['clips'])):
        # Escaped filter graph contains only validated integers.
        parts.append(f'[{i}:v]scale={manifest["width"]}:{manifest["height"]}:force_original_aspect_ratio=decrease,pad={manifest["width"]}:{manifest["height"]}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={manifest["fps"]},format=yuv420p,trim=duration={manifest["clips"][i]["duration"]},setpts=PTS-STARTPTS[v{i}]')
    parts.append(''.join(f'[v{i}]' for i in range(len(manifest['clips']))) + f'concat=n={len(manifest["clips"])}:v=1:a=0[vout]')
    args += ['-filter_complex', ';'.join(parts), '-map', '[vout]']
    if manifest['audio']:
        args += ['-map', f'{len(manifest["clips"])}:a:0', '-c:a', 'aac', '-b:a', '192k', '-shortest']
    else:
        args += ['-an']
    args += ['-c:v', 'h264_nvenc' if gpu else 'libx264', '-pix_fmt', 'yuv420p',
             '-movflags', '+faststart', str(output)]
    return [args]
