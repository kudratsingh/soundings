"""The registry QR code (WO-061 QR addendum): a judge files a plan from a phone.

``GET /api/qr?target=/registry`` returns an SVG QR code of the registry form's URL on the
host the screen was opened on (or ``SOUNDINGS_PUBLIC_BASE_URL``), with the encoded URL in
``X-QR-Payload``; ``GET /api/qr/info`` returns the URL and whether it is a loopback address
a phone cannot reach. Only ``QR_TARGETS`` may be encoded, so this is not an open link
generator. Display only: nothing here reads or touches the ladder (CLAUDE.md 2.2).
"""

from __future__ import annotations

import io
import os
from typing import Annotated
from urllib.parse import urlsplit

import segno
from fastapi import APIRouter, HTTPException, Query, Request, Response

router = APIRouter(tags=["qr"])

QR_TARGETS: frozenset[str] = frozenset({"/registry"})
PUBLIC_BASE_ENV = "SOUNDINGS_PUBLIC_BASE_URL"
LAN_ENV = "SOUNDINGS_LAN"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def qr_base_url(request: Request) -> str:
    """``$SOUNDINGS_PUBLIC_BASE_URL`` when set, else the request's own base URL (no probing)."""
    env = os.environ.get(PUBLIC_BASE_ENV, "").strip()
    base = env if env else str(request.base_url)
    return base.rstrip("/")


def qr_svg(url: str) -> bytes:
    """The QR code of ``url`` as SVG; byte-identical for the same URL."""
    buf = io.BytesIO()
    segno.make(url, error="m", micro=False).save(buf, kind="svg", scale=4, border=2, xmldecl=False)
    return buf.getvalue()


def _target(target: str) -> str:
    if target not in QR_TARGETS:
        raise HTTPException(status_code=422, detail=f"target must be one of {sorted(QR_TARGETS)}")
    return target


@router.get("/api/qr")
def qr_code(request: Request, target: Annotated[str, Query()] = "") -> Response:
    url = qr_base_url(request) + _target(target)
    return Response(
        content=qr_svg(url),
        media_type="image/svg+xml",
        headers={"X-QR-Payload": url, "Cache-Control": "no-store"},
    )


@router.get("/api/qr/info")
def qr_info(request: Request, target: Annotated[str, Query()] = "") -> dict[str, object]:
    url = qr_base_url(request) + _target(target)
    host = urlsplit(url).hostname or ""
    lan = os.environ.get(LAN_ENV) == "1"  # `make dev-lan`: the screen shows a banner
    return {"url": url, "loopback": host in LOOPBACK_HOSTS, "lan": lan}


__all__ = ["QR_TARGETS", "qr_base_url", "qr_svg", "router"]
