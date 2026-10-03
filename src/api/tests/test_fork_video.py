"""Video play upsert."""

from django.urls import reverse

from app.models import MediaTypes, Status, VideoPlay

from .base import FloppyApiTestCase


class VideoPlayApiTests(FloppyApiTestCase):
    """A second post with the same external id updates the play."""

    def test_upsert_raises_progress_and_completes(self):
        """20% stays in progress. 85% completes the same play. No provider calls."""
        url = reverse(
            "api_video_play",
            kwargs={"source": "youtube", "media_id": "dQw4w9WgXcQ"},
        )
        payload = {
            "title": "Ranking EVERY Trader Joe's Pumpkin Product",
            "channel": "Beyond Babish",
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "lengthSeconds": 1000,
            "progressSeconds": 200,
            "externalId": "youtube:dQw4w9WgXcQ:2026-10-02",
        }
        self._metadata_mock.reset_mock()
        first = self.client.post(url, payload, format="json", **self.auth_headers)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.data["status"], Status.IN_PROGRESS.value)

        payload["progressSeconds"] = 850
        second = self.client.post(url, payload, format="json", **self.auth_headers)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.data["status"], Status.COMPLETED.value)
        self.assertEqual(VideoPlay.objects.count(), 1)
        self._metadata_mock.assert_not_called()

        history = self.client.get(
            reverse("api_history"),
            {"media_type": MediaTypes.VIDEO.value, "flat": "1"},
            **self.auth_headers,
        )
        self.assertEqual(history.status_code, 200)
        self.assertIn(payload["title"], str(history.data))
