"""Sphinx configuration for swarph-mesh.

v0.7.3 — first auto-docs build. The public contract surface is enumerated
in ``DEPRECATIONS.md`` (Tier 1/2/3); this Sphinx build surfaces those
primitives via autodoc/autosummary so third-party node implementers
have a single browsable reference.

Pairs with the ``DEPRECATIONS.md`` policy: anything documented here is
in the contract; anything underscored-private is not.
"""

from __future__ import annotations

import os
import sys

# So autodoc can import the package directly out of src/ without install.
sys.path.insert(0, os.path.abspath("../src"))

# -- Project information -----------------------------------------------------

project = "swarph-mesh"
author = "Pierre Samson + Claude Opus"
copyright = "2026, Pierre Samson + Claude Opus"

# Pull version from package __init__ at build time so docs and pyproject
# never drift.
def _read_version() -> str:
    try:
        from swarph_mesh import __version__  # noqa: WPS433
        return __version__
    except Exception:  # pragma: no cover — defensive only
        return "0.0.0"


release = _read_version()
version = ".".join(release.split(".")[:2])

# -- General configuration ---------------------------------------------------

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    "myst_parser",
]

source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}

templates_path = ["_templates"]
exclude_patterns: list[str] = ["_build", "Thumbs.db", ".DS_Store"]
master_doc = "index"

# -- autodoc / autosummary ---------------------------------------------------

autosummary_generate = True
autodoc_member_order = "bysource"
autodoc_typehints = "description"
autodoc_default_options = {
    "members": True,
    "inherited-members": False,
    "show-inheritance": True,
    "undoc-members": False,
    # Underscored symbols are explicitly NOT contract surface per
    # DEPRECATIONS.md "NOT part of the contract".
    "private-members": False,
    "special-members": False,
}
napoleon_google_docstring = True
napoleon_numpy_docstring = False

# -- intersphinx -------------------------------------------------------------

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "pydantic": ("https://docs.pydantic.dev/latest/", None),
}

# Suppress myst xref warnings on relative .md links inside included
# top-level files (CHANGELOG.md / DEPRECATIONS.md / SECURITY.md cross-link
# each other via ``./FILE.md`` — those resolve on GitHub view, which is
# the primary view of those files; rewriting to absolute URLs would harm
# the GitHub experience). The Sphinx site is a secondary view; broken
# anchors there are an acceptable tradeoff.
suppress_warnings = ["myst.xref_missing"]

# -- HTML output -------------------------------------------------------------

html_theme = "furo"
html_static_path = ["_static"]
html_title = f"swarph-mesh v{release}"
html_theme_options = {
    "footer_icons": [
        {
            "name": "GitHub",
            "url": "https://github.com/BrainSurfing-tech/swarph-mesh",
            "html": "",
            "class": "fa-brands fa-github",
        },
    ],
}

# -- MyST (markdown) ---------------------------------------------------------

myst_enable_extensions = [
    "colon_fence",
    "deflist",
    "fieldlist",
    "tasklist",
]
