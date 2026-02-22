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
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .config import Config
from .identify import identify_document
from .metrics import vision_fallbacks_total
from .session import PaperlessSession
from .utils import LOGGER
from .vision import export_training_example, vision_identify_document

click_log.basic_config(LOGGER)


async def _background_identify(
    cfg: Config, document_id: int, *, execute: bool, vision_fallback: bool
) -> None:
    try:
        async with PaperlessSession(cfg) as s:
            doc = await s.lookup_document(document_id)

            # Try pdfrenamer-based identification first.
            original_tags = list(doc.tags)
            await identify_document(execute=execute, session=s, doc=doc)

            # If the identified tag was added, pdfrenamer succeeded — we're done.
            if doc.tags != original_tags:
                return

            if not vision_fallback:
                return

            vision_fallbacks_total.inc()

            # Reload the document to get a clean state for vision identification.
            doc = await s.lookup_document(document_id)
            LOGGER.info(
                "pdfrenamer did not identify document %d, falling back to vision",
                document_id,
            )

            identified_doc = await vision_identify_document(
                execute=execute, session=s, doc=doc
            )
            if identified_doc and execute:
                await s.update_document(identified_doc)
                LOGGER.info(f"Document {doc.id} '{doc.title}' updated via vision.")
    except Exception:
        LOGGER.exception(f"Background identification failed for {document_id}")


async def _background_learn(cfg: Config, document_id: int) -> None:
    try:
        async with PaperlessSession(cfg) as s:
            doc = await s.lookup_document(document_id)
            await export_training_example(
                session=s,
                doc=doc,
                examples_dir=cfg.vision_examples_dir,
            )
    except Exception:
        LOGGER.exception(f"Background learn failed for {document_id}")


async def _extract_document_id(
    request: web.Request,
) -> tuple[int, None] | tuple[None, web.Response]:
    """Parse and validate the request body, returning (document_id, None) or (None, error_response)."""
    if request.method != "POST":
        return None, web.Response(status=405, text="Method Not Allowed")

    try:
        payload = await request.json()
    except Exception:
        return None, web.Response(status=400, text="Invalid or missing JSON body")

    if not isinstance(payload, dict) or "document" not in payload:
        return None, web.Response(status=400, text="Missing 'document' field")

    document_raw = payload["document"]
    if not isinstance(document_raw, str):
        return None, web.Response(status=400, text="'document' must be a string")

    pattern = request.app.get("document_url_pattern")
    if pattern is None:
        return None, web.Response(
            status=500, text="Server misconfigured: missing URL pattern"
        )

    m = pattern.fullmatch(document_raw)
    if not m:
        return None, web.Response(
            status=400, text="Unable to extract document id from 'document' field"
        )

    return int(m.group("document_id")), None


async def metrics_handler(request: web.Request) -> web.Response:
    body = generate_latest()
    return web.Response(body=body, headers={"Content-Type": CONTENT_TYPE_LATEST})


async def learn_handler(request: web.Request) -> web.Response:
    document_id, error = await _extract_document_id(request)
    if error is not None:
        return error
    assert document_id is not None

    cfg: Config = request.app["cfg"]

    asyncio.create_task(_background_learn(cfg, document_id))

    return web.Response(status=200, text="Accepted")


async def identify_handler(request: web.Request) -> web.Response:
    document_id, error = await _extract_document_id(request)
    if error is not None:
        return error
    assert document_id is not None

    execute = bool(request.app.get("execute", False))
    vision_fallback = bool(request.app.get("vision_fallback", False))
    cfg: Config = request.app["cfg"]

    # Schedule the background task and return immediately.
    asyncio.create_task(
        _background_identify(
            cfg, document_id, execute=execute, vision_fallback=vision_fallback
        )
    )

    return web.Response(status=200, text="Accepted")


def create_app(
    cfg: Config, *, execute: bool = False, vision_fallback: bool = False
) -> web.Application:
    # Load renamers once at startup — they register themselves globally.
    load_all_renamers()

    app = web.Application()
    app["cfg"] = cfg
    app["execute"] = execute
    app["vision_fallback"] = vision_fallback
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

    app.add_routes(
        [
            web.post(r"/identify", identify_handler),
            web.post(r"/learn", learn_handler),
            web.get(r"/metrics", metrics_handler),
        ]
    )
    return app


@click.command()
@click_log.simple_verbosity_option(LOGGER)
@click.option(
    "--execute/--no-execute",
    is_flag=True,
    default=False,
    help="If --execute is passed the identification will apply changes.",
)
@click.option(
    "--vision-fallback/--no-vision-fallback",
    is_flag=True,
    default=False,
    help="Fall back to VLM-based vision identification when pdfrenamer fails.",
)
def main(*, execute: bool, vision_fallback: bool) -> None:
    cfg = Config.from_file()
    LOGGER.info(
        f"Starting webapp (execute={execute}, vision_fallback={vision_fallback})"
    )
    app = create_app(cfg, execute=execute, vision_fallback=vision_fallback)
    web.run_app(app, host="0.0.0.0", port=8080)
