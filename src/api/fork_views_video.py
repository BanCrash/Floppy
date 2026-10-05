"""Upsert a video play. The caller already applied the 45 second bar."""

import datetime
from http import HTTPStatus as HTTP  # noqa: N814

from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework.response import Response
from rest_framework.views import APIView

from app.models import Item, MediaTypes, Status, Video

from .helpers import check_source_type


def _end_date_from_external_id(external_id):
    """Use the trailing YYYY-MM-DD on the external id, else now.

    @param external_id - `youtube:<videoId>:<day>`.
    @returns Aware datetime at noon UTC on that day, or now.
    """
    day = external_id.rsplit(":", 1)[-1]
    parsed = parse_date(day)
    if not parsed:
        return timezone.now()
    return timezone.make_aware(datetime.datetime.combine(parsed, datetime.time(12, 0)))


class VideoPlayView(APIView):
    """POST /api/v1/videos/<source>/<media_id>/plays/."""

    def post(self, request, source, media_id):
        """Create the video if needed and upsert the play by external id."""
        if not check_source_type(MediaTypes.VIDEO.value, source):
            return Response(
                {"detail": f"Cannot query `{source}` for video media type"},
                status=HTTP.BAD_REQUEST,
            )

        external_id = str(request.data.get("externalId") or request.data.get("external_id") or "").strip()
        title = str(request.data.get("title") or "").strip()
        if not external_id or not title:
            return Response(
                {"detail": "title and externalId are required"},
                status=HTTP.BAD_REQUEST,
            )
        try:
            progress_seconds = int(request.data.get("progressSeconds") or request.data.get("progress_seconds") or 0)
            length_seconds = int(request.data.get("lengthSeconds") or request.data.get("length_seconds") or 0)
        except (TypeError, ValueError):
            return Response({"detail": "seconds must be integers"}, status=HTTP.BAD_REQUEST)

        # The item is shared by every user who tracks this video, so a later
        # post never renames it.
        item, _created = Item.objects.get_or_create(
            media_id=media_id,
            source=source,
            media_type=MediaTypes.VIDEO.value,
            library_media_type=MediaTypes.VIDEO.value,
            defaults={"title": title},
        )

        video, _video_created = Video.objects.get_or_create(
            item=item,
            user=request.user,
            defaults={
                # The model defaults to Completed, which would fill the
                # progress bar on creation. The first report decides.
                "status": Status.IN_PROGRESS.value,
                "channel": str(request.data.get("channel") or ""),
                "watch_url": str(request.data.get("url") or ""),
                "length_seconds": max(length_seconds, 0),
            },
        )
        video.channel = str(request.data.get("channel") or video.channel)
        video.watch_url = str(request.data.get("url") or video.watch_url)
        if length_seconds:
            video.length_seconds = length_seconds

        play, created = video.upsert_play(
            external_id,
            max(progress_seconds, 0),
            end_date=_end_date_from_external_id(external_id),
        )
        return Response(
            {"status": video.status, "external_id": play.external_id},
            status=HTTP.CREATED if created else HTTP.OK,
        )
