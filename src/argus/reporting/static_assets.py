"""Versioned assets for local report hosting."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

ASSETS_DIR = Path(__file__).parent / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"


@dataclass(frozen=True)
class StaticAsset:
    """One immutable packaged asset."""

    name: str
    path: Path
    version: str
    content_type: str


FONT_FACES: tuple[tuple[str, int, str], ...] = ()

def _version(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


_ASSETS: dict[str, StaticAsset] = {
    path.name: StaticAsset(
        name=path.name,
        path=path,
        version=_version(path),
        content_type="font/woff2",
    )
    for path in sorted(FONTS_DIR.glob("*.woff2"))
}


def assets() -> tuple[StaticAsset, ...]:
    """Return every packaged immutable asset in stable order."""
    return tuple(_ASSETS[name] for name in sorted(_ASSETS))


def url(name: str, *, base_path: str = "/_assets") -> str:
    """Return the content-addressed URL for ``name``."""
    asset = _ASSETS[name]
    return (
        f"{base_path.rstrip('/')}/{asset.version}/{asset.name}"
    )


def resolve(request_path: str) -> StaticAsset | None:
    """Resolve a local ``/_assets/<hash>/<name>`` URL.

    The hash is validated. Serving new bytes for an old immutable URL would
    poison browser and intermediary caches.
    """
    parts = request_path.strip("/").split("/")
    if len(parts) != 3 or parts[0] != "_assets":
        return None
    asset = _ASSETS.get(parts[2])
    if asset is None or parts[1] != asset.version:
        return None
    return asset


def font_face_css(*, base_path: str = "/_assets") -> str:
    """Render font-face rules for any optional packaged fonts."""
    return "".join(
        f"@font-face{{font-family:'{family}';font-weight:{weight};"
        "font-style:normal;font-display:swap;"
        f"src:url('{url(file, base_path=base_path)}') format('woff2')}}\n"
        for family, weight, file in FONT_FACES
    )


def preload_links(*, base_path: str = "/_assets") -> str:
    """Preload the body font faces used during first paint."""
    return "".join(
        '<link rel="preload" as="font" type="font/woff2" crossorigin '
        f'href="{url(file, base_path=base_path)}">'
        for family, _weight, file in FONT_FACES
        if _weight == 400
    )
