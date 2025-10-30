# SPDX-FileCopyrightText: 2024 Diego Elio Pettenò
#
# SPDX-License-Identifier: MIT

from pathlib import Path

from more_itertools import one
from pdfminer.psparser import PSEOF
from pdfrename.lib.pdf_document import Document as PDFDocument
from pdfrename.lib.renamer import InvalidFilenameError, NameComponents, try_all_renamers
from pdfrename.lib.utils import normalize_account_holder_name

from .default_objects import DefaultCustomField
from .session import PaperlessSession
from .types import CustomFieldValue, Document
from .utils import LOGGER, ensure_correspondent, ensure_document_type


def identify_document(
    *, execute: bool, session: PaperlessSession, doc: Document, tmp_dir: Path
) -> Document | None:
    LOGGER.info("Processing document %d: '%s'", doc.id, doc.title)

    field_account_holder = session.cached_custom_field(
        DefaultCustomField.ACCOUNT_HOLDER
    )
    field_account_number = session.cached_custom_field(
        DefaultCustomField.ACCOUNT_NUMBER
    )
    field_document_number = session.cached_custom_field(
        DefaultCustomField.DOCUMENT_NUMBER
    )

    content = session.retrieve_document(doc.id, original=True)
    tmp_pdf = tmp_dir / f"{doc.id}.pdf"
    tmp_pdf.write_bytes(content)

    try:
        result: NameComponents = one(try_all_renamers(PDFDocument(tmp_pdf, LOGGER)))
        LOGGER.info("Document identified: %r", result)
    except ValueError:
        LOGGER.warning(f"Unable to find unique name for '{doc.title}' ({doc.id})")
        return None
    except (PSEOF, IndexError):
        LOGGER.warning(f"Error processing '{doc.title}' ({doc.id})")
        return None

    normalized_account_holders = (
        session.config.lookup_account_holder(normalize_account_holder_name(name, True))
        for name in result.account_holders
    )
    result.account_holder = tuple(normalized_account_holders)
    result.service_name = session.config.lookup_correspondent(result.service_name)
    result.document_type = session.config.lookup_document_type(result.document_type)

    try:
        doc.title = str(result.render_filename(True, True)).removesuffix(".pdf")
    except InvalidFilenameError as e:
        LOGGER.warning(f"Filename found for '{doc.title}' ({doc.id}) is invalid: {e}")
        return None

    doc.created_date = result.date.strftime("%Y-%m-%d")

    account_holder_name = " & ".join(result.account_holder)

    if execute:
        correspondent = ensure_correspondent(session, result.service_name)
        doc.correspondent = correspondent.id

        document_type = ensure_document_type(session, result.document_type)
        doc.document_type = document_type.id

    # We don't want to overwrite account or document numbers if they're already assigned
    # and we didn't detect them, as they might have been filled in manually.
    overwrite_fields = {field_account_holder.id}
    if result.account_number is not None:
        overwrite_fields.add(field_account_number.id)
    if result.document_number is not None:
        overwrite_fields.add(field_document_number.id)

    doc.custom_field_values = [
        custom_field
        for custom_field in doc.custom_field_values
        if custom_field.field not in overwrite_fields
    ]
    doc.custom_field_values.append(
        CustomFieldValue(field=field_account_holder.id, value=account_holder_name)
    )
    if result.account_number is not None:
        doc.custom_field_values.append(
            CustomFieldValue(field=field_account_number.id, value=result.account_number)
        )
    if result.document_number is not None:
        doc.custom_field_values.append(
            CustomFieldValue(
                field=field_document_number.id, value=result.document_number
            )
        )
    if identified_tag_name := session.config.predefined_tags.get("identified"):
        identified_tag = session.lookup_tag(identified_tag_name)
        doc.tags.append(identified_tag.id)

    return doc
