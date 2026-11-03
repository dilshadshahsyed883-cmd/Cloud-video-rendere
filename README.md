# Cloud Video Renderer V1 — SaladCloud + Desktop/VPS API

**Phase:** GitHub image/build + secure desktop testing first. No VPS_1 deploy and no production jobs until a real GPU smoke render passes.

**Scope:** Image contains FFmpeg (H.264 NVENC capable), Editly 0.14.2, Node, Xvfb and authenticated Python API. No dependency installs at container startup. Concat v0.2.8's native project format is **not yet supported**: a separate tested adapter is required to translate Concat timelines/keyframes. The renderer currently supports a safe JSON subset of sequential videos/images, optional audio, and Editly titles.

## Build/publish

Push `feature/gpu-video-renderer-foundation` -> GitHub Actions `Build & publish SaladCloud renderer (amd64)` -> GHCR immutable SHA tag:

`ghcr.io/dilshadshahsyed883-cmd/cloud-video-rendere:sha-<full commit SHA>`

The image should be selectable as a Custom Container Group in SaladCloud with architecture **linux/amd64**, RTX 3060 Ti 8 GB, 4 CPU / 8 GiB RAM, High Priority, one replica to start. Host GPU availability, NVENC usability and throughput require real SaladCloud testing. GHCR image may be private: SaladCloud must be given a GHCR read-only registry credential if not made public. Do not put a GHCR PAT or Salad API key in the image. Workload data is ephemeral on spot instances.

**Salad settings:** port 8080, protocol HTTP behind Salad Gateway TLS (IPv6 listener enabled), startup probe GET `/health/ready`, liveness GET `/health/live`. **Gateway concurrency: multiple concurrent connections enabled**; do not select “limit each server to a single active connection”, because uploads are concurrent with rendering. Environment variables: `RENDERER_API_TOKEN` (random >=32 chars, secret); `PORT=8080`; `RENDERER_ENCODER=nvenc` (default); `RENDERER_DATA_DIR=/data/jobs`. Do not supply a fixed token in GitHub Actions or in a desktop installer. Require HTTPS on the external gateway endpoint. No static credentials are committed.

## API connection design

**Salad management (server-side only):** SaladCloud API token is used to create/start/stop/inspect container groups. Header `Salad-Api-Key`. Store server-side on trusted VPS/controller, never in PC client or this image. This is **different** from renderer authentication.

**Desktop/VPS rendering:** Client sends `Authorization: Bearer <RENDERER_API_TOKEN>` via HTTPS Salad Container Gateway on port 8080 (port exposed internally; the gateway provides its own HTTPS URL). Select **unauthenticated Container Gateway** for this V1 direct-client setup, because Salad gateway-level authentication requires the Salad API key, which must never go to a Windows desktop. The renderer itself still rejects all non-health requests without its separate bearer token. Use the assigned HTTPS gateway URL; directly exposing worker IPs is not supported. The PC should hold the renderer token in Windows Credential Manager (integration not yet built); VPS should hold it as a secret. If future multi-user clients use VPS proxy, only VPS stores renderer token and validates the signed-in user's job ownership. No production keys in source/repo. For production/public endpoints, prefer a VPS-controlled gateway/proxy with rate limits and per-user session authentication, and keep worker private. The V1 direct HTTPS gateway is limited to controlled single-user testing.

Contract version `renderer.v1`:

- `GET /health/live` and `/health/ready` — public health checks.
- `GET /v1/capabilities` — supported engines, encoding and native Concat importer status (auth).
- `POST /v1/jobs` — create job from JSON manifest; returns UUID.
- `HEAD /v1/jobs/{id}/assets/{filename}` — remote upload offset.
- `PUT /v1/jobs/{id}/assets/{filename}` — binary chunk with `Upload-Offset` and `Content-Length`; max 16 MiB per chunk; SHA-256 checked on completion; supports upload ahead while another job renders.
- `POST /v1/jobs/{id}/start` — enqueue once all assets verified; one render at a time.
- `GET /v1/jobs/{id}` — status: uploading, queued, running, completed, failed, canceled.
- `GET /v1/jobs/{id}/log` — final render logs (tail).
- `GET /v1/jobs/{id}/output` — MP4 with `Range` support for resumed downloads. Verify `output_sha256` before handoff to PC/VPS/Drive.
- `POST /v1/jobs/{id}/cancel` — cancel queued or active render.

**Spot caveat:** Node interruption loses the local workspace. The client/orchestrator must retain original project/asset bytes, resubmit a fresh job, and only mark delivered after checking output hash at destination. Initial V1 does not make uploads durable across node replacement. No automatic cleanup of input assets until verified delivery; explicit cleanup to follow after end-to-end testing.

## Example request

`examples/project.json` is a 2-second still image example. Replace the SHA and byte size with real values. Then:

```bash
curl -fsS -X POST "$RENDERER_URL/v1/jobs" -H "Authorization: Bearer $RENDERER_API_TOKEN" -H 'Content-Type: application/json' --data-binary @examples/project.json
# Upload a.png using 16MiB chunks and Upload-Offset, then POST /v1/jobs/$ID/start.
# Poll GET /v1/jobs/$ID. Download GET /v1/jobs/$ID/output using byte ranges.
```

Run unit tests: `python -m unittest discover -s tests -v`. Optional local CPU image check: set `RENDERER_ENCODER=cpu` and expose only `127.0.0.1:8080`. API smoke tests are not real Nvidia NVENC benchmarks and do not prove native Concat compatibility.

## Security and production gates

- Separate Salad API key / GHCR registry key / renderer bearer token; least privilege.
- Never include keys in screenshots, public issues, Docker layers or GitHub files.
- The direct worker API uses a shared bearer token: place a tenant-aware VPS proxy in front before offering it to multiple users.
- GPU encoder, encode speed, Linux Editly effects, actual Salad gateway reachability and 30-second sample upload/render/download must all pass before any deploy to VPS_1.
- Auto Script remains **OFFLINE**; no VPS production deploy in this branch.
