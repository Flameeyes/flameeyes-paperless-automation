# SPDX-FileCopyrightText: 2024 Diego Elio Pettenò
#
# SPDX-License-Identifier: MIT

import dataclasses
import re
import tempfile
from collections.abc import Sequence
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

click_log.basic_config(LOGGER)


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
def ensure_setup(ctx: click.Context):
    execute = ctx.obj.execute
    cfg = Config.from_file()

    with PaperlessSession(cfg) as s:
        try:
            object_owner = s.default_owner
            all_access_group = s.default_access_group
        except ObjectNotFound as e:
            raise click.ClickException(
                f"Unable to find default object owner or access groups: {e}. Aborting."
            ) from e

        # Now we make sure that all the existing tags, correspondent, and document types
        # are owned by the default owner with the corresponding default access group.
        all_tags = s.tags(full_permissions=True)
        for tag in all_tags:
            changed = False
            assert tag.actual_permissions is not None
            if all_access_group.id not in tag.actual_permissions.change.groups:
                LOGGER.info(f"We should add {all_access_group.name} to '{tag.name}'")
                changed = True
            if tag.owner != object_owner.id:
                LOGGER.info(
                    f"We should change owner of the tag '{tag.name}' to {object_owner.username}"
                )
                changed = True

            if execute and changed:
                tag.owner = object_owner.id
                tag.actual_permissions.change.groups.append(all_access_group.id)
                s.update_tag(tag)

        for correspondent in s.correspondents(full_permissions=True):
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
            if correspondent.owner != object_owner.id:
                LOGGER.info(
                    f"We should change owner of the correspondent '{correspondent.name}' to '{object_owner.username}'"
                )
                changed = True

            if execute and changed:
                correspondent.owner = object_owner.id
                correspondent.actual_permissions.change.groups.append(
                    all_access_group.id
                )
                s.update_correspondent(correspondent)

        for document_type in s.document_types(full_permissions=True):
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
            if document_type.owner != object_owner.id:
                LOGGER.info(
                    f"We should change owner of the document type '{document_type.name}' to {object_owner.username}"
                )
                changed = True

            if execute and changed:
                document_type.owner = object_owner.id
                document_type.actual_permissions.change.groups.append(
                    all_access_group.id
                )
                s.update_document_type(document_type)

        # Now we make sure that the configured objects actually exist.
        for tag in cfg.predefined_tags.values():
            if not s.lookup_tag(tag):
                if execute:
                    s.new_tag(tag, to_slug(tag))
                else:
                    LOGGER.info(f"We should create the tag '{tag}'")

        # This creates the custom fields if we didn't have them already.
        try:
            lookup_account_custom_fields(s)
        except ObjectNotFound:
            if execute:
                ensure_account_custom_fields(s)
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
def identify(ctx, *, documents: Sequence[str]) -> None:
    execute = ctx.obj.execute
    load_all_renamers()
    cfg = Config.from_file()

    doc_url_pattern = re.compile(rf"^{cfg.url}/?documents/(?P<document_id>\d+)(/.*)?")

    with PaperlessSession(cfg) as s:
        for doc_ref in documents:
            try:
                document_id = int(doc_ref)
            except ValueError:
                if not (m := doc_url_pattern.fullmatch(doc_ref)):
                    raise click.UsageError(f"Argument '{doc_ref}' is not recognized!")

                document_id = int(m.group("document_id"))

            doc = s.lookup_document(document_id)
            LOGGER.info(f"Found document: {doc.title}")

            with tempfile.TemporaryDirectory(prefix="paperless") as tmp_dir:
                if identified_doc := identify_document(
                    execute=execute, session=s, doc=doc, tmp_dir=Path(tmp_dir)
                ):
                    if execute:
                        s.update_document(identified_doc)
                        LOGGER.info(f"Document '{doc.title}' updated.")


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
def identify_all(
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

    with PaperlessSession(cfg) as s:
        if exclude_identified and "identified" not in cfg.predefined_tags:
            identified_tag_id = s.lookup_tag(cfg.predefined_tags["identified"]).id
        else:
            identified_tag_id = None

        if exclude_scanned and "scanned" in cfg.predefined_tags:
            scanned_tag_id = s.lookup_tag(cfg.predefined_tags["scanned"]).id
        else:
            scanned_tag_id = None

        excluded_tags = {identified_tag_id, scanned_tag_id}

        if only_inbox:
            inbox_tag_id = s.lookup_tag(cfg.predefined_tags["inbox"]).id

        with tempfile.TemporaryDirectory(prefix="paperless") as tmp_dir_s:
            tmp_dir = Path(tmp_dir_s)

            for doc in s.documents():
                if not doc.mime_type == "application/pdf":
                    continue

                tags = set(doc.tags)
                if tags & excluded_tags:
                    continue

                if only_inbox and inbox_tag_id not in tags:
                    continue

                if identified_doc := identify_document(
                    execute=execute, session=s, doc=doc, tmp_dir=Path(tmp_dir)
                ):
                    if execute:
                        s.update_document(identified_doc)
                        LOGGER.info(f"Document '{doc.title}' updated.")


@main.command
@click.option(
    "--only-inbox / --no-only-inbox",
    is_flag=True,
    default=True,
    help="Whether to only process documents tagged with the configured inbox tag.",
)
@click.pass_context
def sort_scanned(ctx, *, only_inbox: bool) -> None:
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

    with PaperlessSession(cfg) as s:
        if only_inbox:
            inbox_tag_id = s.lookup_tag(cfg.predefined_tags["inbox"]).id
        else:
            inbox_tag_id = None

        if "scanned" in cfg.predefined_tags:
            scanned_tag_id = s.lookup_tag(cfg.predefined_tags["scanned"]).id
        else:
            scanned_tag_id = None

        if "scanned" in cfg.predefined_storage_paths:
            scanned_storage_path = s.lookup_storage_path(
                cfg.predefined_storage_paths["scanned"]
            ).id
        else:
            scanned_storage_path = None

        if "unsorted" in cfg.predefined_storage_paths:
            unsorted_storage_path = s.lookup_storage_path(
                cfg.predefined_storage_paths["unsorted"]
            ).id
        else:
            unsorted_storage_path = None

        for doc in s.documents():
            tags = set(doc.tags)

            if only_inbox and inbox_tag_id not in tags:
                continue

            # No need to re-fetch the document if it's already tagged.
            if scanned_tag_id in tags:
                continue

            metadata = s.retrieve_document_metadata(doc.id)
            producer = metadata.original_producer

            if not (producer and any(sw in producer for sw in cfg.scan_software)):
                continue

            if scanned_tag_id is not None:
                doc.tags.append(scanned_tag_id)

            if doc.storage_path is None or (
                scanned_storage_path is not None
                and doc.storage_path == unsorted_storage_path
            ):
                doc.storage_path = scanned_storage_path

            if execute:
                s.update_document(doc)
                LOGGER.info(f"Document '{doc.title}' updated.")
