import unittest
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

import api_server


class ApiServerTests(unittest.TestCase):
    def setUp(self):
        api_server.worker_running = False

    def test_process_queue_payload_accepts_camel_case(self):
        parsed = api_server._process_queue_payload({
            "bridgeUrl": "https://example.com/api/worker/",
            "workerRunId": "run-1",
            "workerCredential": "x" * 32,
            "maxJobs": "2",
        })
        self.assertEqual(parsed["bridge_url"], "https://example.com/api/worker")
        self.assertEqual(parsed["max_jobs"], 2)

    def test_process_queue_payload_rejects_invalid_max_jobs(self):
        with self.assertRaises(HTTPException) as raised:
            api_server._process_queue_payload({
                "bridge_url": "https://example.com/api/worker",
                "worker_run_id": "run-1",
                "worker_credential": "x" * 32,
                "max_jobs": 0,
            })
        self.assertEqual(raised.exception.status_code, 400)

    def test_process_queue_starts_background_worker(self):
        request = Request({"type": "http", "headers": []})
        payload = {
            "bridge_url": "https://example.com/api/worker",
            "worker_run_id": "run-1",
            "worker_credential": "x" * 32,
        }
        with patch.object(api_server.threading.Thread, "start") as start:
            result = api_server.process_queue(payload, request)
        self.assertEqual(result["status"], "started")
        start.assert_called_once_with()

    def test_process_queue_resets_running_flag_if_thread_cannot_start(self):
        request = Request({"type": "http", "headers": []})
        payload = {
            "bridge_url": "https://example.com/api/worker",
            "worker_run_id": "run-1",
            "worker_credential": "x" * 32,
        }
        with patch.object(
            api_server.threading.Thread, "start", side_effect=RuntimeError("thread failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "thread failed"):
                api_server.process_queue(payload, request)
        self.assertFalse(api_server.worker_running)

    def test_worker_models_are_not_cached_by_default(self):
        with patch.dict(api_server.os.environ, {}, clear=True), \
                patch.object(api_server, "_set_worker_model_cache") as set_cache:
            api_server._prepare_worker_models("run-low-memory")
        set_cache.assert_called_once_with(False)

    def test_worker_model_cache_requires_explicit_opt_in(self):
        with patch.dict(
            api_server.os.environ, {"CACHE_MODELS_DURING_WORKER": "1"}, clear=True
        ), patch.object(api_server, "_set_worker_model_cache") as set_cache:
            api_server._prepare_worker_models("run-cache")
        set_cache.assert_called_once_with(True)


if __name__ == "__main__":
    unittest.main()
