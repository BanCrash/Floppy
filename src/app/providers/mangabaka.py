import logging

import requests
from django.conf import settings
from django.core.cache import cache

from app import helpers, request_timing
from app.models import MediaTypes, Sources
from app.providers import services

logger = logging.getLogger(__name__)

base_url = "https://api.mangabaka.org/v1"
PER_PAGE = 30
# Light novels live in the same database; Floppy has no novel media type.
EXCLUDED_TYPES = ["novel"]
NSFW_CONTENT_RATINGS = ["erotica", "pornographic"]
TAG_WEIGHTS = ("core", "defining")


def handle_error(error):
    """Handle MangaBaka API errors."""
    raise services.ProviderAPIError(Sources.MANGABAKA.value, error)


def search(query, page):
    """Search for manga, manhwa and manhua on MangaBaka."""
    if not query.strip():
        return helpers.format_search_response(page, PER_PAGE, 0, [])

    # The NSFW flag changes the result set server-side, so it belongs in the
    # key: without it, flipping MANGABAKA_NSFW keeps serving the other mode's
    # cached page for the full cache lifetime.
    cache_key = (
        f"search_{Sources.MANGABAKA.value}_{MediaTypes.MANGA.value}_"
        f"nsfw_{settings.MANGABAKA_NSFW}_{query}_{page}"
    )
    data = cache.get(cache_key)

    if data is None:
        params = {
            "q": query,
            "page": page,
            "limit": PER_PAGE,
            "type_not": EXCLUDED_TYPES,
        }
        if not settings.MANGABAKA_NSFW:
            params["not_content_rating"] = NSFW_CONTENT_RATINGS

        try:
            response = services.api_request(
                Sources.MANGABAKA.value,
                "GET",
                f"{base_url}/series/search",
                params=params,
            )
        except requests.exceptions.HTTPError as error:
            handle_error(error)

        results = [
            {
                "media_id": str(series["id"]),
                "source": Sources.MANGABAKA.value,
                "media_type": MediaTypes.MANGA.value,
                "title": series["title"],
                "image": get_image_url(series),
                "year": get_start_year(series),
            }
            for series in response["data"]
        ]

        data = helpers.format_search_response(
            page,
            PER_PAGE,
            response["pagination"]["count"],
            results,
        )

        cache.set(cache_key, data)

    return data


@request_timing.timed_provider_call
def manga(media_id):
    """Get metadata for a manga from MangaBaka."""
    cache_key = f"{Sources.MANGABAKA.value}_{MediaTypes.MANGA.value}_{media_id}"
    data = cache.get(cache_key)

    if data is None:
        try:
            response = services.api_request(
                Sources.MANGABAKA.value,
                "GET",
                f"{base_url}/series/{media_id}",
            )
        except requests.exceptions.HTTPError as error:
            handle_error(error)

        series = response["data"]
        published = series.get("published") or {}

        data = {
            "media_id": media_id,
            "source": Sources.MANGABAKA.value,
            "source_url": series["canonical_url"],
            "media_type": MediaTypes.MANGA.value,
            "title": series["title"],
            "image": get_image_url(series),
            "synopsis": series.get("description") or "No synopsis available.",
            "max_progress": get_max_progress(series),
            "genres": get_genres(series),
            "score": get_score(series),
            "score_count": None,
            "details": {
                "format": (series.get("type") or "").title() or None,
                "start_date": published.get("start_date"),
                "end_date": published.get("end_date"),
                "status": (series.get("status") or "").title() or None,
                "authors": series.get("authors") or None,
                "artists": series.get("artists") or None,
                "tags": get_tags(series),
                "content_rating": (series.get("content_rating") or "").title() or None,
            },
            "related": {"related_manga": [], "recommendations": []},
        }

        cache.set(cache_key, data)

    return data


def is_searchable(metadata):
    """Return whether a series passes the same filters as the search results."""
    details = metadata["details"]
    if (details["format"] or "").lower() in EXCLUDED_TYPES:
        return False
    rating = (details["content_rating"] or "").lower()
    return settings.MANGABAKA_NSFW or rating not in NSFW_CONTENT_RATINGS


def get_image_url(series):
    """Get the cover URL, preferring the grid-sized rendition."""
    cover = series.get("cover") or {}
    url = (cover.get("x350") or {}).get("x1") or (cover.get("raw") or {}).get("url")
    return url or settings.IMG_NONE


def get_start_year(series):
    """Get the year publication began."""
    start_date = (series.get("published") or {}).get("start_date")
    if start_date:
        return int(start_date[:4])
    return series.get("year")


def get_max_progress(series):
    """Get the chapter count once the series is complete."""
    total = series.get("total_chapters")
    if series.get("status") == "completed" and total and str(total).isdigit():
        return int(total)
    return None


def get_genres(series):
    """Return the readable genres for the series."""
    genres = [genre.replace("_", " ").title() for genre in series.get("genres") or []]
    return genres or None


def get_tags(series):
    """Return the tags that define the series, leaving out spoilers."""
    tags = [
        tag["name"]
        for tag in series.get("tags_v2") or []
        if not tag.get("is_genre")
        and not tag.get("is_spoiler")
        and tag.get("weight") in TAG_WEIGHTS
    ]
    return tags or None


def get_score(series):
    """Return the 0-100 MangaBaka rating on Floppy's 0-10 scale."""
    rating = series.get("rating")
    if rating:
        return round(rating / 10, 1)
    return None
