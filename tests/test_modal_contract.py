import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from modal_contract import normalize_run_payload


class ModalContractTests(unittest.TestCase):
    def payload(self):
        return {
            "worker_run_id": "123e4567-e89b-12d3-a456-426614174000",
            "worker_credential": "a" * 64,
            "vercel_origin": "https://id-photo-front.vercel.app",
            "bridge_url": "https://attacker.example/ignored",
            "worker_credential_expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }

    def test_callback_is_derived_from_allowed_origin(self):
        with patch.dict("os.environ", {"ALLOWED_VERCEL_ORIGINS": "https://id-photo-front.vercel.app"}):
            self.assertEqual(normalize_run_payload(self.payload())["bridge_url"], "https://id-photo-front.vercel.app/api/worker")

    def test_arbitrary_origins_and_short_credentials_are_rejected(self):
        for change in [
            {"vercel_origin": "https://attacker.example"},
            {"vercel_origin": "http://127.0.0.1"},
            {"worker_credential": "short"},
            {"worker_run_id": "invalid"},
            {"max_jobs": True},
        ]:
            payload = self.payload()
            payload.update(change)
            with self.assertRaises(ValueError):
                normalize_run_payload(payload)

    def test_expired_credentials_are_rejected_before_gpu_allocation(self):
        payload = self.payload()
        payload["worker_credential_expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with self.assertRaises(ValueError):
            normalize_run_payload(payload)
