# SPDX-FileCopyrightText: 2024 Diego Elio Pettenò
#
# SPDX-License-Identifier: MIT

import asyncio
import dataclasses
import re
from collections.abc import Sequence
from functools import wraps
from pathlib import Path

import click
import click_log
from pdfrename.renamers import load_all_renamers

from .config import Config
from .identify import identify_document
from .session import ObjectNotFound, PaperlessSession
from .utils import (
    LOGGER,
    ensure_account_custom_fields,
    lookup_account_custom_fields,
    to_slug,
)
from .vision import (
    FieldResult,
    compare_results,
    document_to_vision_components,
    export_training_example,
    extract_with_vision,
    vision_identify_document,
)

click_log.basic_config(LOGGER)


def coro(func):
    """Decorator to turn an async coroutine into a sync function by running it with asyncio.run.

    The returned callable is synchronous so it can be used as a Click command handler.
    """

    @wraps(func)
    def _sync(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))

    return _sync


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Options:
    execute: bool


@click.group()
@click_log.simple_verbosity_option(LOGGER)
@click.option(
    "--execute / --no-execute",
    is_flag=True,
    default=False,
    help="If using --execute, automation will apply the configured rules.",
)
@click.pass_context
def main(ctx: click.Context, *, execute: bool) -> None:
    ctx.obj = Options(execute=execute)


@main.command()
@click.pass_context
@coro
async def ensure_setup(ctx: click.Context) -> None:
    execute = ctx.obj.execute
    cfg = Config.from_file()

    async with PaperlessSession(cfg) as s:
        try:
            all_access_group = await s.default_access_group()
        except ObjectNotFound as e:
            raise click.ClickException(
                f"Unable to find default object owner or access groups: {e}. Aborting."
            ) from e

        # Now we make sure that all the existing tags, correspondent, and document types
        # have no owner, with the default access group having change permissions.
        async for tag in s.tags(full_permissions=True):
            changed = False
            assert tag.actual_permissions is not None
            if all_access_group.id not in tag.actual_permissions.change.groups:
                LOGGER.info(f"We should add {all_access_group.name} to '{tag.name}'")
                changed = True
            if tag.owner is not None:
                LOGGER.info(f"We should remove owner of the tag '{tag.name}'")
                changed = True

            if execute and changed:
                tag.owner = None
                tag.actual_permissions.change.groups.add(all_access_group.id)
                await s.update_tag(tag)

        async for correspondent in s.correspondents(full_permissions=True):
            changed = False
            assert correspondent.actual_permissions is not None
            if (
                all_access_group.id
                not in correspondent.actual_permissions.change.groups
            ):
                LOGGER.info(
                    f"We should add {all_access_group.name} to '{correspondent.name}'"
                )
                changed = True
            if correspondent.owner is not None:
                LOGGER.info(
                    f"We should remove owner of the correspondent '{correspondent.name}'"
                )
                changed = True

            if execute and changed:
                correspondent.owner = None
                correspondent.actual_permissions.change.groups.add(all_access_group.id)
                await s.update_correspondent(correspondent)

        async for document_type in s.document_types(full_permissions=True):
            changed = False
            assert document_type.actual_permissions is not None
            if (
                all_access_group.id
                not in document_type.actual_permissions.change.groups
            ):
                LOGGER.info(
                    f"We should add {all_access_group.name} to '{document_type.name}'"
                )
                changed = True
            if document_type.owner is not None:
                LOGGER.info(
                    f"We should remove owner of the document type '{document_type.name}'"
                )
                changed = True

            if execute and changed:
                document_type.owner = None
                document_type.actual_permissions.change.groups.add(all_access_group.id)
                await s.update_document_type(document_type)

        # Now we make sure that the configured objects actually exist.
        for tag_name in cfg.predefined_tags.values():
            assert isinstance(tag_name, str)
            try:
                await s.lookup_tag(tag_name)
            except ObjectNotFound:
                if execute:
                    await s.new_tag(tag_name, to_slug(tag_name))
                else:
                    LOGGER.info(f"We should create the tag '{tag_name}'")

        # This creates the custom fields if we didn't have them already.
        try:
            await lookup_account_custom_fields(s)
        except ObjectNotFound:
            if execute:
                await ensure_account_custom_fields(s)
            else:
                LOGGER.info("We should create the account custom field tags.")


@main.command
@click.pass_context
@click.argument(
    "documents",
    type=str,
    required=True,
    nargs=-1,
)
@coro
async def identify(ctx, *, documents: Sequence[str]) -> None:
    execute = ctx.obj.execute
    load_all_renamers()
    cfg = Config.from_file()

    doc_url_pattern = re.compile(rf"^{cfg.url}/?documents/(?P<document_id>\d+)(/.*)?")

    async with PaperlessSession(cfg) as s:
        for doc_ref in documents:
            try:
                document_id = int(doc_ref)
            except ValueError:
                if not (m := doc_url_pattern.fullmatch(doc_ref)):
                    raise click.UsageError(f"Argument '{doc_ref}' is not recognized!")

                document_id = int(m.group("document_id"))

            doc = await s.lookup_document(document_id)
            LOGGER.info(f"Found document: {doc.title}")

            await identify_document(execute=execute, session=s, doc=doc)


@main.command
@click.option(
    "--exclude-identified / --no-exclude-identified",
    is_flag=True,
    default=True,
    help="Whether to exclude documents already tagged as identified.",
)
@click.option(
    "--exclude-scanned / --no-exclude-scanned",
    is_flag=True,
    default=True,
    help="Whether to exclude documents tagged as scanned.",
)
@click.option(
    "--only-inbox / --no-only-inbox",
    is_flag=True,
    default=True,
    help="Whether to only process documents tagged with the configured inbox tag.",
)
@click.pass_context
@coro
async def identify_all(
    ctx, *, exclude_identified: bool, exclude_scanned: bool, only_inbox: bool
) -> None:
    execute = ctx.obj.execute
    load_all_renamers()
    cfg = Config.from_file()

    if only_inbox and "inbox" not in cfg.predefined_tags:
        raise click.UsageError(
            "Unable to use --only-inbox if no inbox tag is configured."
        )

    if exclude_identified and "identified" not in cfg.predefined_tags is None:
        LOGGER.warning("No identified tag present, will not exclude any document.")

    checkpoint_file = Path("./paperless-automation-checkpoint")

    try:
        last_highest_id = int(checkpoint_file.read_text())
    except Exception:
        last_highest_id = -1

    async with PaperlessSession(cfg) as s:
        required_tags = []
        excluded_tags = []

        if only_inbox:
            inbox_tag = await s.lookup_tag(cfg.predefined_tags["inbox"])
            required_tags.append(inbox_tag)

        if exclude_identified and "identified" not in cfg.predefined_tags:
            identified_tag = await s.lookup_tag(cfg.predefined_tags["identified"])
            excluded_tags.append(identified_tag)

        if exclude_scanned and "scanned" in cfg.predefined_tags:
            scanned_tag = await s.lookup_tag(cfg.predefined_tags["scanned"])
            excluded_tags.append(scanned_tag)

        try:
            async for doc_id in s.search_documents(
                required_tags=required_tags if required_tags else None,
                excluded_tags=excluded_tags if excluded_tags else None,
            ):
                if doc_id <= last_highest_id:
                    continue

                doc = await s.lookup_document(doc_id)
                await identify_document(execute=execute, session=s, doc=doc)

                last_highest_id = max(doc.id, last_highest_id)
        finally:
            checkpoint_file.write_text(f"{last_highest_id}")


@main.command
@click.option(
    "--only-inbox / --no-only-inbox",
    is_flag=True,
    default=True,
    help="Whether to only process documents tagged with the configured inbox tag.",
)
@click.pass_context
@coro
async def sort_scanned(ctx, *, only_inbox: bool) -> None:
    execute = ctx.obj.execute
    load_all_renamers()
    cfg = Config.from_file()

    if only_inbox and "inbox" not in cfg.predefined_tags:
        raise click.UsageError(
            "Unable to use --only-inbox if no inbox tag is configured."
        )

    if not cfg.scan_software:
        raise click.UsageError(
            "Unable to sort scanned documents if no scan software is defined."
        )

    async with PaperlessSession(cfg) as s:
        if only_inbox:
            inbox_tag = await s.lookup_tag(cfg.predefined_tags["inbox"])
        else:
            inbox_tag = None

        if "scanned" in cfg.predefined_tags:
            scanned_tag = await s.lookup_tag(cfg.predefined_tags["scanned"])
        else:
            scanned_tag = None

        if "scanned" in cfg.predefined_storage_paths:
            scanned_storage_path = (
                await s.lookup_storage_path(cfg.predefined_storage_paths["scanned"])
            ).id
        else:
            scanned_storage_path = None

        if "unsorted" in cfg.predefined_storage_paths:
            unsorted_storage_path = (
                await s.lookup_storage_path(cfg.predefined_storage_paths["unsorted"])
            ).id
        else:
            unsorted_storage_path = None

        async for doc in s.documents(
            required_tags=(inbox_tag,) if inbox_tag else None,
            excluded_tags=(scanned_tag,) if scanned_tag else None,
        ):
            metadata = await s.retrieve_document_metadata(doc.id)
            producer = metadata.original_producer

            if not (producer and any(sw in producer for sw in cfg.scan_software)):
                continue

            if scanned_tag is not None:
                doc.tags.append(scanned_tag.id)

            if doc.storage_path is None or (
                scanned_storage_path is not None
                and doc.storage_path == unsorted_storage_path
            ):
                doc.storage_path = scanned_storage_path

            if execute:
                await s.update_document(doc)
                LOGGER.info(f"Document {doc.id} '{doc.title}' updated.")


def _parse_document_ref(doc_ref: str, doc_url_pattern: re.Pattern[str]) -> int:
    """Parse a document reference (ID or Paperless URL) to a document ID."""
    try:
        return int(doc_ref)
    except ValueError:
        if not (m := doc_url_pattern.fullmatch(doc_ref)):
            raise click.UsageError(f"Argument '{doc_ref}' is not recognized!")
        return int(m.group("document_id"))


@main.command("vision-identify")
@click.pass_context
@click.argument(
    "documents",
    type=str,
    required=True,
    nargs=-1,
)
@coro
async def vision_identify(ctx: click.Context, *, documents: Sequence[str]) -> None:
    """Identify documents using computer vision (VLM) extraction."""
    execute = ctx.obj.execute
    cfg = Config.from_file()

    doc_url_pattern = re.compile(rf"^{cfg.url}/?documents/(?P<document_id>\d+)(/.*)?")

    async with PaperlessSession(cfg) as s:
        for doc_ref in documents:
            document_id = _parse_document_ref(doc_ref, doc_url_pattern)
            doc = await s.lookup_document(document_id)
            LOGGER.info(f"Found document: {doc.title}")

            if identified_doc := await vision_identify_document(
                execute=execute, session=s, doc=doc
            ):
                if execute:
                    await s.update_document(identified_doc)
                    LOGGER.info(f"Document '{doc.title}' updated.")


@main.command("vision-identify-all")
@click.option(
    "--exclude-identified / --no-exclude-identified",
    is_flag=True,
    default=True,
    help="Whether to exclude documents already tagged as identified.",
)
@click.option(
    "--require-scanned / --no-require-scanned",
    is_flag=True,
    default=True,
    help="Whether to require documents to be tagged as scanned.",
)
@click.option(
    "--only-inbox / --no-only-inbox",
    is_flag=True,
    default=True,
    help="Whether to only process documents tagged with the configured inbox tag.",
)
@click.pass_context
@coro
async def vision_identify_all(
    ctx: click.Context,
    *,
    exclude_identified: bool,
    require_scanned: bool,
    only_inbox: bool,
) -> None:
    """Identify all scanned documents using computer vision (VLM) extraction."""
    execute = ctx.obj.execute
    cfg = Config.from_file()

    if only_inbox and "inbox" not in cfg.predefined_tags:
        raise click.UsageError(
            "Unable to use --only-inbox if no inbox tag is configured."
        )

    if require_scanned and "scanned" not in cfg.predefined_tags:
        raise click.UsageError(
            "Unable to use --require-scanned if no scanned tag is configured."
        )

    async with PaperlessSession(cfg) as s:
        required_tags = []
        excluded_tags = []

        if only_inbox:
            inbox_tag = await s.lookup_tag(cfg.predefined_tags["inbox"])
            required_tags.append(inbox_tag)

        if exclude_identified:
            if "vision_identified" in cfg.predefined_tags:
                vision_identified_tag = await s.lookup_tag(
                    cfg.predefined_tags["vision_identified"]
                )
                excluded_tags.append(vision_identified_tag)
            if "identified" in cfg.predefined_tags:
                identified_tag = await s.lookup_tag(cfg.predefined_tags["identified"])
                excluded_tags.append(identified_tag)

        if require_scanned:
            scanned_tag = await s.lookup_tag(cfg.predefined_tags["scanned"])
            required_tags.append(scanned_tag)

        async for doc in s.documents(
            required_tags=required_tags if required_tags else None,
            excluded_tags=excluded_tags if excluded_tags else None,
        ):
            if identified_doc := await vision_identify_document(
                execute=execute, session=s, doc=doc
            ):
                if execute:
                    await s.update_document(identified_doc)
                    LOGGER.info(f"Document {doc.id} '{doc.title}' updated.")


@main.command("vision-train")
@click.pass_context
@click.argument(
    "documents",
    type=str,
    required=True,
    nargs=-1,
)
@click.option(
    "--examples-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Directory to export training examples to. Defaults to config value.",
)
@coro
async def vision_train(
    ctx: click.Context,
    *,
    documents: Sequence[str],
    examples_dir: Path | None,
) -> None:
    """Export documents as training examples for VLM few-shot prompting.

    Uses the document's current Paperless metadata (correspondent, document type,
    custom fields) as ground truth. Pass document IDs or Paperless document URLs.
    """
    cfg = Config.from_file()
    effective_examples_dir = examples_dir or cfg.vision_examples_dir

    doc_url_pattern = re.compile(rf"^{cfg.url}/?documents/(?P<document_id>\d+)(/.*)?")

    async with PaperlessSession(cfg) as s:
        for doc_ref in documents:
            document_id = _parse_document_ref(doc_ref, doc_url_pattern)
            doc = await s.lookup_document(document_id)
            LOGGER.info(f"Exporting training example for: {doc.title} ({doc.id})")

            example_path = await export_training_example(
                session=s,
                doc=doc,
                examples_dir=effective_examples_dir,
            )
            click.echo(f"Exported: {example_path}")


@main.command("vision-benchmark")
@click.argument(
    "documents",
    type=str,
    required=True,
    nargs=-1,
)
@click.option(
    "--model",
    "models",
    type=str,
    multiple=True,
    help="Model to benchmark. Can be specified multiple times. Defaults to config.",
)
@click.pass_context
@coro
async def vision_benchmark(
    ctx: click.Context,
    *,
    documents: Sequence[str],
    models: tuple[str, ...],
) -> None:
    """Benchmark VLM extraction accuracy against already-identified documents."""
    cfg = Config.from_file()

    doc_url_pattern = re.compile(rf"^{cfg.url}/?documents/(?P<document_id>\d+)(/.*)?")

    effective_models = models if models else (cfg.vision_model,)

    # Per-model aggregate stats
    model_totals: dict[str, int] = {m: 0 for m in effective_models}
    model_matches: dict[str, int] = {m: 0 for m in effective_models}
    model_times: dict[str, list[float]] = {m: [] for m in effective_models}

    async with PaperlessSession(cfg) as s:
        for doc_ref in documents:
            document_id = _parse_document_ref(doc_ref, doc_url_pattern)
            doc = await s.lookup_document(document_id)

            expected = await document_to_vision_components(s, doc)

            click.echo(f'\nDocument {doc.id} "{doc.title}" — ground truth:')
            click.echo(
                f"  correspondent: {expected.correspondent} | "
                f"type: {expected.document_type} | "
                f"date: {expected.date}"
            )
            click.echo(
                f"  holders: {', '.join(expected.account_holders) or '(none)'} | "
                f"account: {expected.account_number} | "
                f"doc_num: {expected.document_number}"
            )

            pdf_bytes = await s.retrieve_document(doc.id, original=True)

            for model_name in effective_models:
                result, elapsed, _status = await extract_with_vision(
                    pdf_bytes=pdf_bytes,
                    config=cfg,
                    model=model_name,
                )

                model_times[model_name].append(elapsed)

                if result is None:
                    click.echo(f"\n  {model_name} ({elapsed:.0f}s): FAILED")
                    fields = (
                        "correspondent",
                        "document_type",
                        "date",
                        "account_holders",
                        "account_number",
                        "document_number",
                    )
                    model_totals[model_name] += len(fields)
                    continue

                # Apply alias resolution (same as vision_identify_document)
                normalized_holders = tuple(
                    cfg.lookup_account_holder(h) for h in result.account_holders
                )
                result = dataclasses.replace(result, account_holders=normalized_holders)

                if result.correspondent:
                    result = dataclasses.replace(
                        result,
                        correspondent=cfg.lookup_correspondent(result.correspondent),
                    )

                if result.document_type:
                    result = dataclasses.replace(
                        result,
                        document_type=cfg.lookup_document_type(result.document_type),
                    )

                comparison = compare_results(result, expected)
                model_totals[model_name] += len(comparison)
                model_matches[model_name] += sum(
                    1
                    for r in comparison.values()
                    if r in (FieldResult.MATCH, FieldResult.BOTH_NULL)
                )

                click.echo(f"\n  {model_name} ({elapsed:.0f}s):")
                line_parts: list[str] = []
                for field_name, field_result in comparison.items():
                    part = f"{field_name}: {field_result.value.upper()}"
                    if field_result == FieldResult.MISMATCH:
                        got = getattr(result, field_name)
                        part += f" (got {got})"
                    line_parts.append(part)
                # Print in two rows of three
                click.echo(f"    {line_parts[0]}  {line_parts[1]}  {line_parts[2]}")
                if len(line_parts) > 3:
                    click.echo(f"    {'  '.join(line_parts[3:])}")

    # Summary
    if len(documents) > 1 or len(effective_models) > 1:
        click.echo(f"\nSummary ({len(documents)} documents):")
        click.echo(f"  {'Model':<25} | {'Accuracy':>15} | {'Avg Time':>10}")
        for model_name in effective_models:
            total = model_totals[model_name]
            matched = model_matches[model_name]
            pct = (matched / total * 100) if total else 0
            times = model_times[model_name]
            avg_time = sum(times) / len(times) if times else 0
            accuracy = f"{matched}/{total} ({pct:.0f}%)"
            click.echo(f"  {model_name:<25} | {accuracy:>15} | {avg_time:>8.0f}s")


@main.command("merge-correspondents")
@click.pass_context
@click.option(
    "--into",
    "destination_id",
    type=int,
    required=True,
    help="ID of the correspondent to merge into.",
)
@click.argument("source_ids", type=int, nargs=-1, required=True)
@coro
async def merge_correspondents(
    ctx: click.Context, *, destination_id: int, source_ids: Sequence[int]
) -> None:
    """Merge one or more correspondents into a destination correspondent.

    Reassigns all documents from each SOURCE_ID correspondent to the --into
    correspondent, then deletes the now-empty source correspondents.
    """
    execute = ctx.obj.execute
    cfg = Config.from_file()

    async with PaperlessSession(cfg) as s:
        destination = await s.lookup_correspondent_by_id(destination_id)
        click.echo(f"Destination: [{destination.id}] {destination.name}")

        for source_id in source_ids:
            source = await s.lookup_correspondent_by_id(source_id)
            click.echo(f"Merging [{source.id}] {source.name} → {destination.name}")

            count = 0
            async for doc in s.documents_by_correspondent(source_id):
                doc.correspondent = destination_id
                count += 1
                if execute:
                    await s.update_document(doc)
                    LOGGER.info(
                        "Document %d reassigned to %s", doc.id, destination.name
                    )
                else:
                    LOGGER.info(
                        "Would reassign document %d to %s", doc.id, destination.name
                    )

            click.echo(f"  {count} document(s) reassigned.")

            if execute:
                await s.delete_correspondent(source_id)
                click.echo(f"  Deleted correspondent [{source_id}] {source.name}.")
            else:
                click.echo(f"  Would delete correspondent [{source_id}] {source.name}.")


@main.command("merge-tags")
@click.pass_context
@click.option(
    "--into",
    "destination_id",
    type=int,
    required=True,
    help="ID of the tag to merge into.",
)
@click.argument("source_ids", type=int, nargs=-1, required=True)
@coro
async def merge_tags(
    ctx: click.Context, *, destination_id: int, source_ids: Sequence[int]
) -> None:
    """Merge one or more tags into a destination tag.

    Reassigns all documents from each SOURCE_ID tag to the --into tag (adding
    the destination tag and removing the source tag), then deletes the
    now-empty source tags.
    """
    execute = ctx.obj.execute
    cfg = Config.from_file()

    async with PaperlessSession(cfg) as s:
        destination = await s.lookup_tag_by_id(destination_id)
        click.echo(f"Destination: [{destination.id}] {destination.name}")

        for source_id in source_ids:
            source = await s.lookup_tag_by_id(source_id)
            click.echo(f"Merging [{source.id}] {source.name} → {destination.name}")

            count = 0
            async for doc in s.documents_by_tag_id(source_id):
                doc.tags = [t for t in doc.tags if t != source_id]
                if destination_id not in doc.tags:
                    doc.tags.append(destination_id)
                count += 1
                if execute:
                    await s.update_document(doc)
                    LOGGER.info("Document %d retagged to %s", doc.id, destination.name)
                else:
                    LOGGER.info(
                        "Would retag document %d to %s", doc.id, destination.name
                    )

            click.echo(f"  {count} document(s) retagged.")

            if execute:
                await s.delete_tag(source_id)
                click.echo(f"  Deleted tag [{source_id}] {source.name}.")
            else:
                click.echo(f"  Would delete tag [{source_id}] {source.name}.")


@main.command("merge-document-types")
@click.pass_context
@click.option(
    "--into",
    "destination_id",
    type=int,
    required=True,
    help="ID of the document type to merge into.",
)
@click.argument("source_ids", type=int, nargs=-1, required=True)
@coro
async def merge_document_types(
    ctx: click.Context, *, destination_id: int, source_ids: Sequence[int]
) -> None:
    """Merge one or more document types into a destination document type.

    Reassigns all documents from each SOURCE_ID document type to the --into
    document type, then deletes the now-empty source document types.
    """
    execute = ctx.obj.execute
    cfg = Config.from_file()

    async with PaperlessSession(cfg) as s:
        destination = await s.lookup_document_type_by_id(destination_id)
        click.echo(f"Destination: [{destination.id}] {destination.name}")

        for source_id in source_ids:
            source = await s.lookup_document_type_by_id(source_id)
            click.echo(f"Merging [{source.id}] {source.name} → {destination.name}")

            count = 0
            async for doc in s.documents_by_document_type(source_id):
                doc.document_type = destination_id
                count += 1
                if execute:
                    await s.update_document(doc)
                    LOGGER.info(
                        "Document %d reclassified to %s", doc.id, destination.name
                    )
                else:
                    LOGGER.info(
                        "Would reclassify document %d to %s", doc.id, destination.name
                    )

            click.echo(f"  {count} document(s) reclassified.")

            if execute:
                await s.delete_document_type(source_id)
                click.echo(f"  Deleted document type [{source_id}] {source.name}.")
            else:
                click.echo(f"  Would delete document type [{source_id}] {source.name}.")
