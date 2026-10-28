"""Nearby meal lookup: framework-free domain used by the MaiBot plugin."""

from .amap import AmapClient, AmapError, map_url
from .candidates import CampusCatalog, CampusRequest, CandidateError
from .export import render_html, render_json, render_markdown, write_exports
from .library import MealLibrary
from .models import (
    Evidence,
    MealCandidate,
    MealOutcome,
    MealReport,
    MealRequest,
    MealState,
    MenuCatalog,
    MenuEntry,
    MenuItem,
    Place,
)
from .progress import SearchProgress
from .render import describe, describe_campus, map_message
from .service import MealService, NearbyLookup
from .sources import ResearchSources

__all__ = [
    "AmapClient",
    "AmapError",
    "CandidateError",
    "CampusCatalog",
    "CampusRequest",
    "Evidence",
    "MealCandidate",
    "MealLibrary",
    "MenuItem",
    "MenuEntry",
    "MenuCatalog",
    "MealOutcome",
    "MealReport",
    "MealRequest",
    "MealService",
    "MealState",
    "NearbyLookup",
    "Place",
    "ResearchSources",
    "SearchProgress",
    "describe",
    "describe_campus",
    "map_message",
    "map_url",
    "render_html",
    "render_json",
    "render_markdown",
    "write_exports",
]
