"""Per-media-type tile subtitle profiles.

One JSON object on ``User`` stores which subtitle lines each media type shows.
Missing or partial saves fall back to ``default_profile``, which matches the
shared card's current year line.
"""

from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from app.models.choices import MediaTypes

VERSION = 1
DISPLAY_HOVER = "hover"
DISPLAY_ALWAYS = "always"
DISPLAY_DORMANT = "dormant"
DISPLAY_CHOICES = (DISPLAY_HOVER, DISPLAY_ALWAYS)
FIELD_DISPLAY_CHOICES = (DISPLAY_HOVER, DISPLAY_DORMANT)
PERSON = "person"

_MEDIA_TYPES = tuple(choice.value for choice in MediaTypes)
PROFILE_TYPES = (*_MEDIA_TYPES, PERSON)

_ALL_MEDIA = frozenset(_MEDIA_TYPES)
_EXTRA = "extra_query"
_CHEAP = "cheap"


def _field(label, types, cost=_CHEAP):
    """Build one registry entry."""
    return {"label": label, "types": frozenset(types), "cost": cost}


TILE_FIELDS = {
    "release_year": _field("Release year", _ALL_MEDIA),
    "episode_code": _field("Season and episode", {"episode", "season", "tv"}),
    "genres": _field("Genres", _ALL_MEDIA),
    "runtime": _field("Runtime", _ALL_MEDIA),
    "progress": _field("Progress", _ALL_MEDIA),
    "series_position": _field("Series position", {"book"}),
    "status": _field("Status", _ALL_MEDIA),
    "rating": _field("Your rating", _ALL_MEDIA),
    "last_played": _field("Last played", _ALL_MEDIA),
    "synopsis": _field("Synopsis", _ALL_MEDIA),
    "artist": _field("Artist", {"music"}, _EXTRA),
    "album": _field("Album", {"music"}, _EXTRA),
    "track_number": _field("Track number", {"music"}, _EXTRA),
    "show_name": _field("Show", {"episode", "season"}, _EXTRA),
    "author": _field("Author", {"book", "comic", "manga"}, _EXTRA),
    "role": _field("Role", {PERSON}),
}


def _default_field_ids(media_type):
    """Return the field ids a type shows before the user edits them."""
    if media_type == PERSON:
        return ["role"]
    if media_type == MediaTypes.MUSIC.value:
        return ["artist", "release_year"]
    if media_type == MediaTypes.BOOK.value:
        return ["release_year", "series_position", "progress"]
    if media_type == MediaTypes.EPISODE.value:
        return ["episode_code", "release_year"]
    return ["release_year", "progress"]


def default_profile(media_type):
    """Return the profile that reproduces today's tile for ``media_type``."""
    return {
        "display": DISPLAY_HOVER,
        "fields": list(_default_field_ids(media_type)),
        "options": {"rating": {"hide_zero": False}},
    }


def profiles_from_legacy(display, progress_bar, hide_zero):
    """Seed every type from the three global preference columns."""
    display_value = display if display in DISPLAY_CHOICES else DISPLAY_HOVER
    types = {}
    for media_type in PROFILE_TYPES:
        profile = default_profile(media_type)
        profile["display"] = display_value
        if not progress_bar:
            profile["fields"] = [
                field_id for field_id in profile["fields"] if field_id != "progress"
            ]
        profile["options"] = {"rating": {"hide_zero": bool(hide_zero)}}
        types[media_type] = profile
    return {"version": VERSION, "types": types}


def _saved_types(user):
    """Return the stored type map, or an empty dict when nothing is saved."""
    raw = getattr(user, "tile_metadata", None)
    if not isinstance(raw, dict):
        return {}
    types = raw.get("types")
    if not isinstance(types, dict):
        return {}
    return types


def parse_tile_metadata(raw_payload):
    """Drop unknown types and fields. A bad display becomes hover.

    ``raw_payload`` is a JSON string from the settings form, or a dict.
    """
    payload = raw_payload
    if isinstance(raw_payload, str):
        import json

        try:
            payload = json.loads(raw_payload or "{}")
        except json.JSONDecodeError:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    source = payload.get("types") if isinstance(payload.get("types"), dict) else payload
    types = {}
    for media_type in PROFILE_TYPES:
        entry = source.get(media_type)
        if not isinstance(entry, dict):
            continue
        allowed = {
            field_id
            for field_id, spec in TILE_FIELDS.items()
            if media_type in spec["types"]
        }
        fields = []
        for field_id in entry.get("fields") or []:
            if isinstance(field_id, str) and field_id in allowed and field_id not in fields:
                fields.append(field_id)
        display = entry.get("display")
        if display not in DISPLAY_CHOICES:
            display = DISPLAY_HOVER
        options = entry.get("options") if isinstance(entry.get("options"), dict) else {}
        types[media_type] = {
            "display": display,
            "fields": fields,
            "options": _clean_options(options, allowed),
        }
    return {"version": VERSION, "types": types}


def resolve_profile(user, media_type):
    """Return the saved profile for ``media_type``, or the default."""
    if media_type not in PROFILE_TYPES:
        media_type = MediaTypes.MOVIE.value
    saved = _saved_types(user).get(media_type)
    if not isinstance(saved, dict):
        base = default_profile(media_type)
        if not _saved_types(user):
            base["display"] = _legacy_display(user)
            if not _legacy_progress_bar(user):
                base["fields"] = [
                    field_id for field_id in base["fields"] if field_id != "progress"
                ]
            base["options"]["rating"]["hide_zero"] = _legacy_hide_zero(user)
        return base
    return parse_tile_metadata({"types": {media_type: saved}})["types"].get(
        media_type, default_profile(media_type)
    )


def uses_custom_fields(user, media_type):
    """Return whether the saved field list differs from the type default."""
    saved = _saved_types(user).get(media_type)
    if not isinstance(saved, dict):
        return False
    profile = resolve_profile(user, media_type)
    return profile["fields"] != default_profile(media_type)["fields"]


def uses_line_renderer(user, media_type):
    """Return whether this card should draw one line per field.

    The shared card keeps its old year-and-progress markup until the field
    list changes or a field has its own hover/dormant choice.
    """
    if uses_custom_fields(user, media_type):
        return True
    profile = resolve_profile(user, media_type)
    for field_id in profile["fields"]:
        if _stored_field_display(profile, field_id) is not None:
            return True
    return False


def _clean_options(options, allowed):
    """Keep hide-zero and a hover/dormant choice for known fields."""
    rating = options.get("rating") if isinstance(options.get("rating"), dict) else {}
    cleaned = {"rating": {"hide_zero": bool(rating.get("hide_zero"))}}
    rating_display = _known_field_display(rating.get("display"))
    if rating_display is not None:
        cleaned["rating"]["display"] = rating_display
    for field_id, raw in options.items():
        if field_id == "rating" or field_id not in allowed or not isinstance(raw, dict):
            continue
        display = _known_field_display(raw.get("display"))
        if display is not None:
            cleaned[field_id] = {"display": display}
    return cleaned


def _known_field_display(value):
    """Return a stored per-field mode, or None when the value is not one."""
    if value in FIELD_DISPLAY_CHOICES:
        return value
    return None


def _stored_field_display(profile, field_id):
    """Return the mode saved on this field, ignoring the type-level fallback."""
    raw = (profile.get("options") or {}).get(field_id)
    if not isinstance(raw, dict):
        return None
    return _known_field_display(raw.get("display"))


def field_visibility(profile, field_id):
    """Return ``hover`` or ``dormant`` for one enabled field."""
    stored = _stored_field_display(profile, field_id)
    if stored is not None:
        return stored
    if profile.get("display") == DISPLAY_ALWAYS:
        return DISPLAY_DORMANT
    return DISPLAY_HOVER


def _legacy_display(user):
    value = getattr(user, "media_card_subtitle_display", DISPLAY_HOVER)
    return value if value in DISPLAY_CHOICES else DISPLAY_HOVER


def _legacy_progress_bar(user):
    return getattr(user, "progress_bar", True)


def _legacy_hide_zero(user):
    return bool(getattr(user, "hide_zero_rating", False))


def subtitle_display(user, media_type):
    """Return ``hover`` or ``always`` for this type."""
    return resolve_profile(user, media_type)["display"]


def show_progress_field(user, media_type):
    """Return whether the progress line, and the poster bar, are enabled."""
    return "progress" in resolve_profile(user, media_type)["fields"]


def hides_zero_rating(user, media_type):
    """Return whether a zero score is hidden for this type."""
    profile = resolve_profile(user, media_type)
    return bool(profile["options"]["rating"]["hide_zero"])


def field_enabled(user, media_type, field_id):
    """Return whether this type's profile includes ``field_id``."""
    return field_id in resolve_profile(user, media_type)["fields"]


def extra_query_enabled(user, media_type, field_id):
    """Return whether an extra-query field is on for this type."""
    spec = TILE_FIELDS.get(field_id)
    if spec is None or spec["cost"] != _EXTRA:
        return False
    return field_enabled(user, media_type, field_id)


def _text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _from_obj(obj, name):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _release_year(item, media, user):
    from app.templatetags.app_tags import release_year

    year = release_year(item, media)
    if year:
        return str(year)
    for obj in (item, media):
        begin = _from_obj(obj, "begin_year")
        if begin:
            return str(begin)
        release_date = _from_obj(obj, "release_date")
        if release_date:
            year_prefix = str(release_date).split("-", 1)[0]
            if year_prefix.isdigit():
                return year_prefix
    return None


def _genres(item, media, user):
    genres = _from_obj(item, "genres") or []
    if isinstance(genres, str):
        return _text(genres)
    names = []
    for genre in genres:
        if isinstance(genre, str):
            names.append(genre)
        elif isinstance(genre, dict) and genre.get("name"):
            names.append(genre["name"])
    return ", ".join(names[:3]) or None


def _runtime(item, media, user):
    runtime = _from_obj(item, "runtime") or _from_obj(media, "runtime_display")
    if runtime:
        return _text(runtime)
    minutes = _from_obj(item, "runtime_minutes")
    if minutes:
        return ngettext("%(count)s min", "%(count)s min", int(minutes)) % {
            "count": int(minutes)
        }
    return None


def _progress(item, media, user):
    if media is None:
        return None
    media_type = _from_obj(item, "media_type")
    progress = _from_obj(media, "progress")
    if media_type == MediaTypes.MOVIE.value:
        count = progress or _from_obj(media, "repeats")
        if not count:
            return None
        count = int(count)
        return ngettext("%(count)s play", "%(count)s plays", count) % {"count": count}
    formatted = _from_obj(media, "formatted_progress")
    if formatted:
        return _text(formatted)
    max_progress = _from_obj(media, "max_progress")
    if progress and max_progress:
        if getattr(user, "book_comic_manga_progress_percentage", False) and media_type in {
            MediaTypes.BOOK.value,
            MediaTypes.COMIC.value,
            MediaTypes.MANGA.value,
        }:
            return f"{int(100 * float(progress) / float(max_progress))}%"
        return f"{progress} / {max_progress}"
    if progress:
        return _text(progress)
    return None


def _series_position(item, media, user):
    position = _from_obj(item, "series_position")
    if not position:
        return None
    return f"Book {position:g}" if isinstance(position, float) else f"Book {position}"


def _status(item, media, user):
    return _text(_from_obj(media, "status"))


def _rating(item, media, user):
    score = _from_obj(media, "score")
    if score in (None, "", 0):
        return None
    return str(score)


def _format_episode_code(season_number, episode_number):
    """Return ``S01`` or ``S01 E03``."""
    if season_number is None:
        return None
    if episode_number is None:
        return f"S{int(season_number):02d}"
    return f"S{int(season_number):02d} E{int(episode_number):02d}"


def _show_episode_code(media):
    """Return the show's next episode, or the furthest one it already has."""
    if media is None or not hasattr(media, "seasons"):
        return None
    target = getattr(media, "next_episode_target", None)
    if callable(target):
        found = target()
        if found is not None:
            season_row, episode_number = found
            season_number = _from_obj(getattr(season_row, "item", None), "season_number")
            code = _format_episode_code(season_number, episode_number)
            if code and episode_number is not None:
                return code
    best = None
    for season in media.seasons.all():
        season_number = _from_obj(getattr(season, "item", None), "season_number")
        if not season_number:
            continue
        episodes = getattr(season, "episodes", None)
        if episodes is None:
            continue
        for episode in episodes.all():
            episode_number = _from_obj(getattr(episode, "item", None), "episode_number")
            if episode_number is None:
                continue
            pair = (int(season_number), int(episode_number))
            if best is None or pair > best:
                best = pair
    if best is None:
        return None
    return _format_episode_code(best[0], best[1])


def _episode_code(item, media, user):
    season = _from_obj(item, "season_number")
    episode = _from_obj(item, "episode_number")
    if season is not None and episode is not None:
        return _format_episode_code(season, episode)
    show_code = _show_episode_code(media)
    if show_code:
        return show_code
    return _format_episode_code(season, episode)


def _last_played(item, media, user):
    played = _from_obj(media, "last_played_at")
    return _text(played)


def _synopsis(item, media, user):
    text = _from_obj(item, "synopsis") or _from_obj(media, "synopsis")
    if not text:
        return None
    compact = " ".join(str(text).split())
    return compact[:140] or None


def _named(obj, attr):
    value = _from_obj(obj, attr)
    if value is None:
        return None
    if isinstance(value, str):
        return _text(value)
    return _text(getattr(value, "name", None) or getattr(value, "title", None))


def _artist(item, media, user):
    for obj in (item, media):
        name = _named(obj, "artist")
        if name:
            return name
        album = _from_obj(obj, "album")
        name = _named(album, "artist")
        if name:
            return name
        name = _text(_from_obj(obj, "artist_name"))
        if name:
            return name
    return None


def _album(item, media, user):
    for obj in (item, media):
        name = _named(obj, "album")
        if name:
            return name
    return None


def _track_number(item, media, user):
    number = _from_obj(item, "track_number") or _from_obj(media, "track_number")
    return _text(number)


def _show_name(item, media, user):
    for obj in (item, media):
        name = _named(obj, "show") or _text(_from_obj(obj, "show_title"))
        if name:
            return name
    return None


def _author(item, media, user):
    return _named(item, "author") or _text(_from_obj(item, "author_name"))


def _role(item, media, user):
    return _text(
        _from_obj(item, "role")
        or _from_obj(item, "character")
        or _from_obj(item, "department")
    )


_RENDERERS = {
    "release_year": _release_year,
    "genres": _genres,
    "runtime": _runtime,
    "progress": _progress,
    "series_position": _series_position,
    "status": _status,
    "rating": _rating,
    "episode_code": _episode_code,
    "last_played": _last_played,
    "synopsis": _synopsis,
    "artist": _artist,
    "album": _album,
    "track_number": _track_number,
    "show_name": _show_name,
    "author": _author,
    "role": _role,
}


def tile_lines(user, media_type, item=None, media=None):
    """Return enabled subtitle lines, skipping blanks.

    Each line is ``{"text", "dormant"}``. Dormant lines stay visible when the
    card is at rest. Hover lines appear with the card hover.
    """
    profile = resolve_profile(user, media_type)
    lines = []
    for field_id in profile["fields"]:
        renderer = _RENDERERS.get(field_id)
        if renderer is None:
            continue
        text = renderer(item, media, user)
        if text:
            lines.append(
                {
                    "text": text,
                    "dormant": field_visibility(profile, field_id) == DISPLAY_DORMANT,
                }
            )
    return lines


def type_label(media_type):
    """Return the name the rest of the app uses for this type."""
    if media_type == PERSON:
        return _("Person")
    return _(MediaTypes(media_type).label)


def editor_catalog():
    """Return the settings-page payload: types, fields, and which are extra."""
    types = []
    for media_type in PROFILE_TYPES:
        fields = []
        for field_id, spec in TILE_FIELDS.items():
            if media_type not in spec["types"]:
                continue
            fields.append(
                {
                    "id": field_id,
                    "label": spec["label"],
                    "extra": spec["cost"] == _EXTRA,
                }
            )
        types.append(
            {"id": media_type, "label": type_label(media_type), "fields": fields}
        )
    return types


OMIT = object()
ABSORBED_PREFERENCE_FIELDS = (
    "media_card_subtitle_display",
    "progress_bar",
    "hide_zero_rating",
)


def _scalar(profile, field_name, media_type):
    """Read one legacy preference from a type profile.

    Types whose default has no progress field do not vote on ``progress_bar``.
    """
    if field_name == "media_card_subtitle_display":
        return profile["display"]
    if field_name == "progress_bar":
        if "progress" not in default_profile(media_type)["fields"]:
            return None
        return "progress" in profile["fields"]
    return bool(profile["options"]["rating"]["hide_zero"])


def _write_scalar(profile, field_name, value, media_type):
    """Write one legacy preference onto a type profile."""
    if field_name == "media_card_subtitle_display":
        profile["display"] = value if value in DISPLAY_CHOICES else DISPLAY_HOVER
        return
    if field_name == "progress_bar":
        if "progress" not in default_profile(media_type)["fields"]:
            return
        fields = list(profile["fields"])
        if value and "progress" not in fields:
            fields.append("progress")
        if not value:
            fields = [field_id for field_id in fields if field_id != "progress"]
        profile["fields"] = fields
        return
    profile["options"]["rating"]["hide_zero"] = bool(value)


def absorbed_preference_value(user, field_name):
    """Return the shared value, or ``OMIT`` when types disagree."""
    if not _saved_types(user):
        defaults = {
            "media_card_subtitle_display": _legacy_display(user),
            "progress_bar": _legacy_progress_bar(user),
            "hide_zero_rating": _legacy_hide_zero(user),
        }
        return defaults[field_name]
    values = []
    for media_type in PROFILE_TYPES:
        value = _scalar(resolve_profile(user, media_type), field_name, media_type)
        if value is not None:
            values.append(value)
    if values and all(value == values[0] for value in values):
        return values[0]
    if not values:
        return True
    return OMIT


def apply_absorbed_preference(user, field_name, value):
    """Copy one legacy preference onto every type profile."""
    if _saved_types(user):
        base = user.tile_metadata
    else:
        base = profiles_from_legacy(
            getattr(user, "media_card_subtitle_display", DISPLAY_HOVER),
            getattr(user, "progress_bar", True),
            getattr(user, "hide_zero_rating", False),
        )
    parsed = parse_tile_metadata(base)
    for media_type in PROFILE_TYPES:
        profile = parsed["types"].setdefault(media_type, default_profile(media_type))
        _write_scalar(profile, field_name, value, media_type)
    user.tile_metadata = parsed
