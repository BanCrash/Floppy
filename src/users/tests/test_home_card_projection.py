"""Home cards must not hydrate the item columns they never render.

A custom-list row loads every item in the list, hydrates media for all of
them, sorts in Python and only then slices roughly ten cards. On an
11,008-item list the dominant cost is `Item.watch_providers` -- TMDB's
availability for every region it knows -- loaded twice, once for the items
and once again through their media.
"""

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from app.models import Item, MediaTypes, Movie, Sources, Status
from lists.models import CustomList, CustomListItem
from users.home_screen import _custom_list_row_window
from users.models import HomeScreenRow, HomeScreenRowTypeChoices

ITEM_COUNT = 10
PROVIDERS = {f"REGION{index}": [{"provider_id": index}] for index in range(139)}


class HomeCardProjectionTests(TestCase):
    """The column no home card renders must not be selected."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="home-projection",
            password="12345",
        )
        cls.custom_list = CustomList.objects.create(
            name="Projection List", owner=cls.user,
        )
        for index in range(ITEM_COUNT):
            item = Item.objects.create(
                media_id=f"projection-movie-{index}",
                source=Sources.TMDB.value,
                media_type=MediaTypes.MOVIE.value,
                title=f"Projection Movie {index}",
                watch_providers=PROVIDERS,
            )
            Movie.objects.create(
                item=item, user=cls.user, status=Status.COMPLETED.value,
            )
            CustomListItem.objects.create(custom_list=cls.custom_list, item=item)
        cls.row = HomeScreenRow.objects.create(
            user=cls.user,
            media_type=MediaTypes.MOVIE.value,
            row_type=HomeScreenRowTypeChoices.CUSTOM_LIST,
            custom_list=cls.custom_list,
        )

    def test_watch_providers_is_never_selected_for_a_custom_list_row(self):
        """Neither the item query nor the media query may load it."""
        with CaptureQueriesContext(connection) as captured:
            entries, _total = _custom_list_row_window(self.user, self.row, 0, 100, seed=0)

        self.assertEqual(len(entries), ITEM_COUNT)
        loading = [
            query["sql"]
            for query in captured.captured_queries
            if '"watch_providers"' in query["sql"]
        ]
        self.assertEqual(
            len(loading),
            0,
            f"{len(loading)} home-row queries loaded watch_providers",
        )

    def test_the_cards_still_carry_what_they_render(self):
        """Projection is invisible to the row: same items, same media."""
        entries, _total = _custom_list_row_window(self.user, self.row, 0, 100, seed=0)

        titles = sorted(entry.item.title for entry in entries)
        self.assertEqual(titles[0], "Projection Movie 0")
        self.assertTrue(all(entry.media is not None for entry in entries))
        self.assertTrue(all(entry.show_progress_controls for entry in entries))
