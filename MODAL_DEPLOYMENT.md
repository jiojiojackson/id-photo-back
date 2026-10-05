# Oracle and Modal backends

The existing Oracle service stays available. The frontend task page chooses one
backend for the entire pending queue; Oracle is the default.

The Modal app is deployed as `id-photo-gpu` in workspace `jiojiojackson`:

- Dashboard: https://modal.com/apps/jiojiojackson/main/deployed/id-photo-gpu
- Control endpoint: https://jiojiojackson--id-photo-gpu-web.modal.run
- GPU: one NVIDIA L4, with BiRefNet v1 Lite and RetinaFace using CUDA.
- GPU containers scale to zero when idle; the idle window is 60 seconds.
- Queue inference uses the same `result.hd` output as Oracle.

## Deployment

Install `requirements-modal.txt`, configure your Modal profile with
`modal token set --profile jiojiojackson`, and deploy:

```sh
modal deploy modal_app.py
```

The named Modal Secret `id-photo-modal-control` contains:

- `MODAL_BACKEND_TOKEN`: dedicated random HTTP access token.
- `ALLOWED_VERCEL_ORIGINS`: comma-separated callback origins; production is
  `https://id-photo-front.vercel.app`.

Vercel production uses `MODAL_API_URL` and the same `MODAL_BACKEND_TOKEN`.
The Modal account token stays outside Vercel and the repositories.

## Worker lifecycle

The CPU control endpoint authenticates and validates a wake request before
spawning a detached GPU function. Duplicate run IDs are not dispatched twice.
A CPU observer reports initialization, execution, and timeout failures back to
the authenticated Worker Bridge. GPU cold starts have a 10 minute startup
window; GPU heartbeats transition the run into the normal 120 second liveness
window. Per-job heartbeats continue extending image-processing leases.

Status and health requests use CPU functions and never allocate a GPU. The
cleanup route checks both configured backends and the database worker state
before deleting photos. Existing bounded R2 cleanup and retry behavior remain.
