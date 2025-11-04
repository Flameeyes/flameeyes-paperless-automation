# SPDX-FileCopyrightText: 2025 Diego Elio Pettenò
#
# SPDX-License-Identifier: MIT

"""Webhook-compatible pdfrenamer webapp."""

import asyncio
import re

import click
import click_log
from aiohttp import web
from pdfrename.renamers import load_all_renamers

from .config import Config
from .identify import identify_document
from .session import PaperlessSession
from .utils import LOGGER

click_log.basic_config(LOGGER)


async def _background_identify(cfg: Config, document_id: int, execute: bool) -> None:
    try:
        async with PaperlessSession(cfg) as s:
            doc = await s.lookup_document(document_id)
            await identify_document(execute=execute, session=s, doc=doc)
    except Exception:
        LOGGER.exception(f"Background identification failed for {document_id}")


async def identify_handler(request: web.Request) -> web.Response:
    # Only accept POST with JSON body containing a string `document_id`.
    if request.method != "POST":
        return web.Response(status=405, text="Method Not Allowed")

    try:
        payload = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid or missing JSON body")

    if not isinstance(payload, dict) or "document" not in payload:
        return web.Response(status=400, text="Missing 'document' field")

    document_raw = payload["document"]
    if not isinstance(document_raw, str):
        return web.Response(status=400, text="'document' must be a string")

    # Expect a document URL and extract the id using the precompiled pattern.
    pattern = request.app.get("document_url_pattern")
    if pattern is None:
        return web.Response(
            status=500, text="Server misconfigured: missing URL pattern"
        )

    m = pattern.fullmatch(document_raw)
    if not m:
        return web.Response(
            status=400, text="Unable to extract document id from 'document' field"
        )

    document_id = int(m.group("document_id"))

    execute = bool(request.app.get("execute", False))
    cfg: Config = request.app["cfg"]

    # Schedule the background task and return immediately.
    asyncio.create_task(_background_identify(cfg, document_id, execute))

    return web.Response(status=200, text="Accepted")


def create_app(cfg: Config, *, execute: bool = False) -> web.Application:
    # Load renamers once at startup — they register themselves globally.
    load_all_renamers()

    app = web.Application()
    app["cfg"] = cfg
    app["execute"] = execute
    # Pre-build the document URL pattern from the configured base URL so any
    # misconfiguration is detected at startup rather than at request time.
    base_url = cfg.url
    try:
        document_url_pattern = re.compile(
            rf"^{re.escape(base_url)}/?documents/(?P<document_id>\d+)(/.*)?"
        )
    except re.error as exc:  # pragma: no cover - defensive
        raise RuntimeError(
            f"Invalid configured base URL for document matching: {exc}"
        ) from exc

    app["document_url_pattern"] = document_url_pattern

    app.add_routes([web.post(r"/identify", identify_handler)])
    return app


@click.command()
@click_log.simple_verbosity_option(LOGGER)
@click.option(
    "--execute/--no-execute",
    is_flag=True,
    default=False,
    help="If --execute is passed the identification will apply changes.",
)
def main(*, execute: bool) -> None:
    cfg = Config.from_file()
    LOGGER.info(f"Starting webapp (execute={execute})")
    app = create_app(cfg, execute=execute)
    web.run_app(app, host="0.0.0.0", port=8080)
