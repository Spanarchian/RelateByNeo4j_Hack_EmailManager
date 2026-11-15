"""Sphinx configuration (local builds and Read the Docs)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from email_service import __version__

project = "Gmail Onboarding Assistant"
author = "Gmail Onboarding Assistant contributors"
copyright = "2026, Gmail Onboarding Assistant contributors"
version = release = __version__

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.viewcode",
]
source_suffix = {".rst": "restructuredtext", ".md": "markdown"}
myst_enable_extensions = ["colon_fence"]
myst_heading_anchors = 3
exclude_patterns = ["_build"]

autodoc_member_order = "bysource"
autodoc_typehints = "description"
autodoc_default_options = {"members": True, "undoc-members": False}

html_theme = "sphinx_rtd_theme"
html_static_path = ["_static"]
html_title = f"{project} {version}"
