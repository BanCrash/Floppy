"""Personal series ordering with explicit, reviewable history reconciliation."""

import requests
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from app.models import TV, Item
from app.models.episode_order import EpisodeOrder
from app.providers import credentials, episode_orders, services
from app.services import episode_ordering, metadata_resolution


def owned_tv(user, tv_id):
    """Resolve a tracked series only within the requesting user's library."""
    return get_object_or_404(
        TV.objects.select_related("item", "active_episode_order"),
        pk=tv_id, user=user,
    )


def available_orders(tv, user):
    """Discover configured providers using only established series identities."""
    orders, errors = [], []
    for provider in ("tmdb", "tvdb"):
        series_id = metadata_resolution.resolve_provider_media_id(
            tv.item, provider, route_media_type="tv", persist_links=False,
        )
        if not series_id or not credentials.is_configured(provider, user=user):
            continue
        try:
            orders.extend(episode_orders.list_orders(provider, series_id, user=user))
        except (services.ProviderAPIError, requests.RequestException, ValueError, KeyError):
            errors.append(f"{provider.upper()} episode orders are temporarily unavailable.")
    return orders, errors


def selected_order(tv, user, provider, key):
    """Bind a submitted order to the series' verified provider ID."""
    if provider not in {"tmdb", "tvdb"}:
        message = "Select TMDB or TVDB."
        raise ValueError(message)
    series_id = metadata_resolution.resolve_provider_media_id(
        tv.item, provider, route_media_type="tv",
    )
    if not series_id:
        message = "This series has no verified identity for that provider."
        raise ValueError(message)
    return episode_ordering.load_order(tv.item, provider, series_id, key, user=user)


def _form_resolutions(data, preview):
    watches = {str(row["id"]): row for row in preview["watches"]}
    groups = {}
    for watch_id in watches:
        anchor = data.get(f"combine_{watch_id}") or watch_id
        if anchor not in watches:
            message = "Choose a viewing from this preview."
            raise ValueError(message)
        groups.setdefault(anchor, []).append(int(watch_id))
    resolutions = []
    for anchor, watch_ids in groups.items():
        if int(anchor) not in watch_ids:
            message = "Combined viewings must point to a viewing kept separate in its own row."
            raise ValueError(message)
        archive = data.get(f"archive_{anchor}") == "on"
        resolution = {
            "watch_ids": watch_ids,
            "episode_ids": [] if archive else data.getlist(f"episodes_{anchor}"),
            "archive": archive,
        }
        if len(watch_ids) > 1:
            # The form explicitly identifies this viewing as the metadata source.
            resolution["fields"] = {
                field: watches[anchor][field] for field in episode_ordering.WATCH_FIELDS
            }
        resolutions.append(resolution)
    return resolutions


def _preview_context(order, preview):
    titles = dict(Item.objects.filter(
        pk__in=[row["item_id"] for row in preview["watches"]],
    ).values_list("pk", "title"))
    proposed = {row["watch_ids"][0]: row["episode_ids"] for row in preview["resolutions"]}
    for watch_id, suggestion in preview["suggestions"].items():
        proposed[watch_id] = proposed.get(watch_id) or [suggestion["episode_id"]]
    return {
        "order": order, "preview": preview,
        "watches": [{**row, "title": titles[row["item_id"]],
                     "proposed": proposed[row["id"]]} for row in preview["watches"]],
        "episodes": order.catalogue["episodes"],
    }


@login_required
@require_http_methods(["GET", "POST"])
def episode_ordering_settings(request, tv_id):
    """Preview and apply episode ordering without changing another user's items."""
    tv = owned_tv(request.user, tv_id)
    context = {"tv": tv}
    status = 200
    if request.method == "POST":
        try:
            if request.POST.get("action") == "apply":
                order = get_object_or_404(EpisodeOrder, pk=request.POST.get("order_id"), show=tv.item)
                preview = episode_ordering.preview_change(tv, order)
                episode_ordering.apply_change(
                    tv, order, token=request.POST.get("token", ""),
                    resolutions=_form_resolutions(request.POST, preview),
                )
                messages.success(request, "Episode ordering updated.")
                return redirect("episode_ordering_settings", tv_id=tv.pk)
            order = selected_order(
                tv, request.user, request.POST.get("provider"), request.POST.get("key"),
            )
            context.update(_preview_context(order, episode_ordering.preview_change(tv, order)))
        except (ValueError, ValidationError) as error:
            context["error"] = str(error)
            status = 400
        except (services.ProviderAPIError, requests.RequestException, KeyError):
            context["error"] = "Provider data is unavailable. Your current ordering has been preserved."
            status = 503
    context["orders"], context["provider_errors"] = available_orders(tv, request.user)
    return render(request, "app/episode_ordering.html", context, status=status)
