# Synchronous /process-queue lifecycle

This branch changes `/process-queue` to execute the queue worker inside the HTTP request, matching the lifecycle of the historical `/generate` endpoint.

The request remains open while the Worker Run processes jobs. After the queue is empty and `/finish` has completed, model cleanup runs and `_process_jobs()` returns. The HTTP response is then sent to the caller. No `os._exit()` is used and no background queue worker thread is created.

Goal: let Lightning observe the same request lifecycle as `/generate`, so request completion—not process termination—marks the end of the Worker Run.
