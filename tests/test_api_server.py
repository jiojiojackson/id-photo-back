import unittest
from unittest.mock import Mock, patch

import requests

from fastapi import HTTPException
from starlette.requests import Request

import api_server


class ApiServerTests(unittest.TestCase):
    def setUp(self):
        api_server.worker_running = False
        api_server.active_worker_run_id = None

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

    def test_health_reports_actual_active_run(self):
        api_server.worker_running = True
        api_server.active_worker_run_id = "existing-run"
        self.assertEqual(api_server.health()["worker_run_id"], "existing-run")

    def test_conflicting_run_is_not_acknowledged_as_started(self):
        api_server.worker_running = True
        api_server.active_worker_run_id = "existing-run"
        payload = {"bridge_url": "https://example.com/api/worker", "worker_run_id": "new-run", "worker_credential": "x" * 32}
        with patch.object(api_server.threading.Thread, "start") as start:
            with self.assertRaises(HTTPException) as raised:
                api_server.process_queue(payload, Request({"type": "http", "headers": []}))
        self.assertEqual(raised.exception.status_code, 409)
        start.assert_not_called()

    def test_storage_permissions_and_invalid_images_do_not_retry(self):
        for status in [400, 401, 403, 404, 413]:
            response = requests.Response()
            response.status_code = status
            self.assertFalse(api_server._retryable_job_error(requests.HTTPError(response=response)))
        self.assertFalse(api_server._retryable_job_error(ValueError("No face detected")))
        for status in [429, 500, 503]:
            response = requests.Response()
            response.status_code = status
            self.assertTrue(api_server._retryable_job_error(requests.HTTPError(response=response)))
        self.assertTrue(api_server._retryable_job_error(requests.Timeout()))

    def test_error_does_not_expose_presigned_url(self):
        text = api_server._safe_error(ValueError("403 for https://storage.example/photo?X-Amz-Signature=secret"))
        self.assertNotIn("secret", text)
        self.assertNotIn("X-Amz", text)

    def test_download_failure_reports_terminal_failure_and_finishes(self):
        job = {"id": "job-1", "inputUrl": "https://storage.example/input?secret=hidden", "outputUrl": "https://storage.example/output"}
        next_response = Mock(status_code=200)
        next_response.json.return_value = {"status": "job", "job": job}
        fail_response = Mock()
        fail_response.json.return_value = {"ok": True, "status": "failed"}
        empty_response = Mock(status_code=200)
        empty_response.json.return_value = {"status": "empty"}
        finish_response = Mock()
        denied = requests.Response()
        denied.status_code = 403
        download = Mock()
        download.raise_for_status.side_effect = requests.HTTPError("403 https://storage.example/input?secret=hidden", response=denied)
        with patch.object(api_server, "_prepare_worker_models"), \
                patch.object(api_server, "_finish_worker_models"), \
                patch.object(api_server, "_release_per_job_memory"), \
                patch.object(api_server, "_heartbeat_loop"), \
                patch.object(requests, "get", return_value=download), \
                patch.object(requests, "post", side_effect=[next_response, fail_response, empty_response, finish_response]) as post:
            processed = api_server._process_jobs("https://example.com/api/worker", "run-1", "x" * 32, None)
        self.assertEqual(processed, 1)
        failure = post.call_args_list[1].kwargs["json"]
        self.assertFalse(failure["retryable"])
        self.assertNotIn("hidden", failure["error"])
        self.assertEqual(post.call_args_list[-1].args[0], "https://example.com/api/worker/finish")
        self.assertFalse(api_server.worker_running)
        self.assertIsNone(api_server.active_worker_run_id)


if __name__ == "__main__":
    unittest.main()
