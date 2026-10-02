"""YouTube-style video tracker and one play per watched day."""

from django.db import models
from django.utils import timezone
from model_utils import FieldTracker

from app.models.choices import Status
from app.models.media import Media

COMPLETED_RATIO = 0.8


class Video(Media):
    """A video that is not a movie. YouTube first, other sources later."""

    tracker = FieldTracker()
    channel = models.CharField(max_length=255, blank=True, default="")
    watch_url = models.URLField(max_length=500, blank=True, default="")
    length_seconds = models.PositiveIntegerField(default=0)

    class Meta:
        """One row per user and item."""

        unique_together = ("item", "user")

    def upsert_play(self, external_id, progress_seconds, end_date=None):
        """Create or raise the play for this external id.

        @param external_id - Idempotency key, `youtube:<id>:<day>`.
        @param progress_seconds - Resume position in seconds.
        @param end_date - When the watch counts. Defaults to now.
        @returns The play and whether it was created.
        """
        when = end_date or timezone.now()
        completed = (
            self.length_seconds > 0
            and progress_seconds >= COMPLETED_RATIO * self.length_seconds
        )
        self.progress = progress_seconds
        self.status = Status.COMPLETED.value if completed else Status.IN_PROGRESS.value
        self.end_date = when
        self.save(update_fields=["progress", "status", "end_date", "channel", "watch_url", "length_seconds"])

        play = self.plays.filter(external_id=external_id).first()
        created = play is None
        if created:
            play = VideoPlay.objects.create(
                video=self,
                external_id=external_id,
                progress=progress_seconds,
                end_date=when,
            )
        else:
            play.progress = progress_seconds
            play.end_date = when
            play.save(update_fields=["progress", "end_date"])
        return play, created


class VideoPlay(models.Model):
    """One viewing of a video, keyed by external id."""

    created_at = models.DateTimeField(auto_now_add=True)
    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name="plays")
    external_id = models.CharField(max_length=255)
    progress = models.PositiveIntegerField(default=0)
    end_date = models.DateTimeField()

    class Meta:
        """Same external id updates one play."""

        constraints = [
            models.UniqueConstraint(
                fields=["video", "external_id"],
                name="app_videoplay_unique_video_external_id",
            ),
        ]
