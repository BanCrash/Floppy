from unittest.mock import MagicMock, patch

import requests
from django.conf import settings
from django.core.cache import cache
from django.test import TestCase, override_settings

from app.models import MediaTypes, Sources
from app.providers import mangabaka, services


def _series(**overrides):
    series = {
        "id": 84926,
        "state": "active",
        "canonical_url": "https://mangabaka.org/series/84926/Berserk",
        "title": "Berserk",
        "cover": {
            "raw": {"url": "https://cdn.mangabaka.org/raw.jpg"},
            "x350": {"x1": "https://cdn.mangabaka.org/350.jpg", "x2": None},
        },
        "authors": ["MIURA Kentarou"],
        "artists": ["MIURA Kentarou"],
        "description": "Guts, a former mercenary.",
        "status": "releasing",
        "type": "manga",
        "rating": 91.46,
        "total_chapters": "380",
        "published": {"start_date": "1989-08-25", "end_date": None},
        "genres": ["action", "boys_love"],
        "tags_v2": [
            {"name": "Dark Fantasy", "is_genre": False, "weight": "core"},
            {"name": "Hero Dies", "is_spoiler": True, "weight": "core"},
            {"name": "Gore", "is_genre": False, "weight": "incidental"},
            {"name": "Action", "is_genre": True, "weight": "defining"},
        ],
    }
    series.update(overrides)
    return series


def _search_page(count=1):
    return {
        "status": 200,
        "pagination": {"count": count, "page": 1, "limit": 30},
        "data": [_series()],
    }


class MangaBakaProviderTests(TestCase):
    """MangaBaka provider parsing, filters and errors (API mocked)."""

    def setUp(self):
        cache.clear()

    @patch("app.providers.mangabaka.services.api_request")
    def test_search_parses_results(self, mock_request):
        mock_request.return_value = _search_page(count=61)

        data = mangabaka.search("berserk", 2)

        self.assertEqual(data["page"], 2)
        self.assertEqual(data["total_results"], 61)
        self.assertEqual(
            data["results"],
            [
                {
                    "media_id": "84926",
                    "source": Sources.MANGABAKA.value,
                    "media_type": MediaTypes.MANGA.value,
                    "title": "Berserk",
                    "image": "https://cdn.mangabaka.org/350.jpg",
                    "year": 1989,
                },
            ],
        )
        params = mock_request.call_args.kwargs["params"]
        self.assertEqual(params["q"], "berserk")
        self.assertEqual(params["page"], 2)

    @patch("app.providers.mangabaka.services.api_request")
    def test_search_excludes_novels_and_adult_by_default(self, mock_request):
        mock_request.return_value = _search_page()

        mangabaka.search("berserk", 1)

        params = mock_request.call_args.kwargs["params"]
        self.assertEqual(params["type_not"], ["novel"])
        self.assertEqual(params["not_content_rating"], ["erotica", "pornographic"])

    @override_settings(MANGABAKA_NSFW=True)
    @patch("app.providers.mangabaka.services.api_request")
    def test_search_nsfw_setting_drops_the_adult_filter(self, mock_request):
        mock_request.return_value = _search_page()

        mangabaka.search("berserk", 1)

        params = mock_request.call_args.kwargs["params"]
        self.assertNotIn("not_content_rating", params)
        self.assertEqual(params["type_not"], ["novel"])

    @patch("app.providers.mangabaka.services.api_request")
    def test_search_cached_per_nsfw_state(self, mock_request):
        mock_request.return_value = _search_page()

        mangabaka.search("probe", 1)
        mangabaka.search("probe", 1)
        with override_settings(MANGABAKA_NSFW=True):
            mangabaka.search("probe", 1)

        self.assertEqual(mock_request.call_count, 2)

    @patch("app.providers.mangabaka.services.api_request")
    def test_blank_search_makes_no_request(self, mock_request):
        data = mangabaka.search("  ", 1)

        self.assertEqual(data["results"], [])
        mock_request.assert_not_called()

    @patch("app.providers.mangabaka.services.api_request")
    def test_search_without_cover_uses_placeholder(self, mock_request):
        page = _search_page()
        page["data"][0]["cover"] = {"raw": {"url": None}, "x350": {"x1": None}}
        page["data"][0]["published"] = {"start_date": None}
        page["data"][0]["year"] = 2001
        mock_request.return_value = page

        result = mangabaka.search("berserk", 1)["results"][0]

        self.assertEqual(result["image"], settings.IMG_NONE)
        self.assertEqual(result["year"], 2001)

    @patch("app.providers.mangabaka.services.api_request")
    def test_manga_metadata(self, mock_request):
        mock_request.return_value = {"status": 200, "data": _series()}

        data = mangabaka.manga("84926")

        self.assertEqual(data["media_id"], "84926")
        self.assertEqual(data["source"], Sources.MANGABAKA.value)
        self.assertEqual(data["source_url"], "https://mangabaka.org/series/84926/Berserk")
        self.assertEqual(data["title"], "Berserk")
        self.assertEqual(data["synopsis"], "Guts, a former mercenary.")
        self.assertEqual(data["genres"], ["Action", "Boys Love"])
        self.assertEqual(data["score"], 9.1)
        self.assertIsNone(data["max_progress"])
        self.assertEqual(data["details"]["format"], "Manga")
        self.assertEqual(data["details"]["status"], "Releasing")
        self.assertEqual(data["details"]["start_date"], "1989-08-25")
        self.assertEqual(data["details"]["authors"], ["MIURA Kentarou"])
        # Spoiler, genre and incidental tags stay out.
        self.assertEqual(data["details"]["tags"], ["Dark Fantasy"])
        self.assertEqual(
            mock_request.call_args.args[2],
            "https://api.mangabaka.org/v1/series/84926",
        )

    @patch("app.providers.mangabaka.services.api_request")
    def test_completed_series_sets_max_progress(self, mock_request):
        mock_request.return_value = {
            "status": 200,
            "data": _series(status="completed", total_chapters="71"),
        }

        self.assertEqual(mangabaka.manga("1")["max_progress"], 71)

    @patch("app.providers.mangabaka.services.api_request")
    def test_sparse_series_still_parses(self, mock_request):
        mock_request.return_value = {
            "status": 200,
            "data": _series(
                description=None,
                rating=None,
                genres=[],
                tags_v2=None,
                authors=None,
                artists=None,
                status="completed",
                total_chapters=None,
            ),
        }

        data = mangabaka.manga("2")

        self.assertEqual(data["synopsis"], "No synopsis available.")
        self.assertIsNone(data["score"])
        self.assertIsNone(data["genres"])
        self.assertIsNone(data["max_progress"])
        self.assertIsNone(data["details"]["tags"])
        self.assertIsNone(data["details"]["authors"])

    @patch("app.providers.mangabaka.services.api_request")
    def test_http_error_becomes_provider_error(self, mock_request):
        response = MagicMock(status_code=404)
        mock_request.side_effect = requests.exceptions.HTTPError(response=response)

        with self.assertRaises(services.ProviderAPIError):
            mangabaka.manga("999")

    @patch("app.providers.mangabaka.services.api_request")
    def test_direct_id_lookup_follows_search_filters(self, mock_request):
        cases = [
            (_series(), True, False),
            (_series(type="novel"), False, False),
            (_series(content_rating="erotica"), False, True),
            (_series(content_rating="pornographic"), False, True),
            (_series(content_rating="suggestive"), True, False),
        ]
        for series, searchable, nsfw_unlocks in cases:
            with self.subTest(type=series["type"], rating=series.get("content_rating")):
                cache.clear()
                mock_request.return_value = {"status": 200, "data": series}
                metadata = mangabaka.manga("1")

                self.assertEqual(mangabaka.is_searchable(metadata), searchable)
                with override_settings(MANGABAKA_NSFW=True):
                    self.assertEqual(
                        mangabaka.is_searchable(metadata),
                        searchable or nsfw_unlocks,
                    )

    @patch("app.providers.mangabaka.manga")
    def test_search_by_id_hides_filtered_series(self, mock_manga):
        mock_manga.return_value = {
            "title": "Adult Series",
            "details": {"format": "Manga", "content_rating": "Pornographic"},
        }

        result = services.search_by_id(
            MediaTypes.MANGA.value,
            "84926",
            Sources.MANGABAKA.value,
        )

        self.assertIsNone(result)
