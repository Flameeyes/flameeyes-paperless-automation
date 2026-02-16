# SPDX-FileCopyrightText: 2026 Diego Elio Pettenò
#
# SPDX-License-Identifier: MIT

import dataclasses
import datetime
import json
import re
import time
from pathlib import Path

import ollama

from .config import Config
from .default_objects import DefaultCustomField
from .session import PaperlessSession
from .types import CustomFieldValue, Document
from .utils import LOGGER, ensure_correspondent, ensure_document_type

SYSTEM_PROMPT = """\
You are a document analysis assistant. Your task is to extract structured \
information from scanned document images.

Extract the following fields and return them as a JSON object:
- "correspondent": The name of the organization or person who sent or issued \
the document (bank, utility company, government agency, etc.). Return null if \
not found.
- "document_type": The type of document (e.g. "Statement", "Invoice", "Bill", \
"Letter", "Notice"). Return null if not found.
- "date": The primary date of the document in ISO 8601 format (YYYY-MM-DD). \
This is usually the issue date or statement date. Return null if not found.
- "account_holders": A JSON array of strings naming the account holder(s) or \
recipient(s) of the document. Return an empty array if not found.
- "account_number": The account number or reference number tied to the \
account. Return null if not found.
- "document_number": A specific document number, invoice number, or reference \
number for this individual document. Return null if not found.

Return ONLY a valid JSON object with these exact keys. Do not include any \
explanation or additional text.\
"""

USER_PROMPT = "Analyze this document and extract the structured fields."


@dataclasses.dataclass(slots=True, kw_only=True)
class VisionComponents:
    correspondent: str | None = None
    document_type: str | None = None
    date: datetime.date | None = None
    account_holders: tuple[str, ...] = ()
    account_number: str | None = None
    document_number: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class FewShotExample:
    images: list[bytes]
    expected_json: str


def pdf_to_images(
    pdf_bytes: bytes, *, dpi: int = 300, max_pages: int = 2
) -> list[bytes]:
    """Render up to max_pages pages of a PDF as PNG bytes."""
    import fitz

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    images: list[bytes] = []
    for page_num in range(min(max_pages, doc.page_count)):
        page = doc.load_page(page_num)
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat)
        images.append(pix.tobytes("png"))
    return images


def load_few_shot_examples(
    examples_dir: Path, max_examples: int = 3
) -> list[FewShotExample]:
    """Load the most recent N examples from the examples directory."""
    if not examples_dir.exists():
        return []

    example_dirs = sorted(
        (d for d in examples_dir.iterdir() if d.is_dir()),
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )[:max_examples]

    examples: list[FewShotExample] = []
    for example_dir in example_dirs:
        expected_path = example_dir / "expected.json"
        if not expected_path.exists():
            continue
        expected_json = expected_path.read_text()
        images: list[bytes] = []
        for page_path in sorted(example_dir.glob("page_*.png")):
            images.append(page_path.read_bytes())
        if images:
            examples.append(FewShotExample(images=images, expected_json=expected_json))

    return examples


def _parse_vision_response(raw_text: str) -> VisionComponents | None:
    """Parse the VLM JSON response into VisionComponents."""
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE)
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        LOGGER.warning("Failed to parse VLM response as JSON: %s\nRaw: %s", e, raw_text)
        return None

    date_val = None
    if raw_date := data.get("date"):
        try:
            date_val = datetime.date.fromisoformat(raw_date)
        except (ValueError, TypeError):
            LOGGER.warning("Could not parse date %r", raw_date)

    account_holders_raw = data.get("account_holders") or []
    if isinstance(account_holders_raw, str):
        account_holders_raw = [account_holders_raw]

    return VisionComponents(
        correspondent=data.get("correspondent") or None,
        document_type=data.get("document_type") or None,
        date=date_val,
        account_holders=tuple(str(h) for h in account_holders_raw),
        account_number=data.get("account_number") or None,
        document_number=data.get("document_number") or None,
    )


async def extract_with_vision(
    *,
    pdf_bytes: bytes,
    config: Config,
) -> VisionComponents | None:
    """Call the Ollama VLM to extract document fields from PDF bytes."""
    images = pdf_to_images(
        pdf_bytes,
        max_pages=config.vision_pages_to_process,
    )
    if not images:
        LOGGER.warning("No pages rendered from PDF")
        return None

    few_shot = load_few_shot_examples(
        config.vision_examples_dir,
        max_examples=config.vision_max_few_shot_examples,
    )

    messages: list[ollama.Message] = [
        ollama.Message(role="system", content=SYSTEM_PROMPT),
    ]

    # Few-shot examples as user/assistant turn pairs (first page only per example)
    for example in few_shot:
        messages.append(
            ollama.Message(
                role="user",
                content=USER_PROMPT,
                images=[ollama.Image(value=example.images[0])],
            )
        )
        messages.append(
            ollama.Message(
                role="assistant",
                content=example.expected_json,
            )
        )

    # The actual document (all rendered pages)
    messages.append(
        ollama.Message(
            role="user",
            content=USER_PROMPT,
            images=[ollama.Image(value=img) for img in images],
        )
    )

    client = ollama.AsyncClient(host=config.vision_ollama_url)

    LOGGER.debug(
        "Sending %d messages (%d few-shot examples, %d document pages) to %s",
        len(messages),
        len(few_shot),
        len(images),
        config.vision_model,
    )
    start_time = time.monotonic()

    try:
        response = await client.chat(
            model=config.vision_model,
            messages=messages,
            options=ollama.Options(temperature=0.0, num_ctx=16384),
        )
    except ollama.ResponseError as e:
        LOGGER.error("Ollama request failed: %s", e)
        return None

    elapsed = time.monotonic() - start_time
    raw_text = response.message.content or ""
    LOGGER.debug("VLM response in %.1fs: %s", elapsed, raw_text)
    return _parse_vision_response(raw_text)


async def vision_identify_document(
    *, execute: bool, session: PaperlessSession, doc: Document
) -> Document | None:
    """Identify a document using VLM-based extraction."""
    LOGGER.info("Processing document %d with vision: '%s'", doc.id, doc.title)

    field_account_holder = await session.cached_custom_field(
        DefaultCustomField.ACCOUNT_HOLDER
    )
    field_account_number = await session.cached_custom_field(
        DefaultCustomField.ACCOUNT_NUMBER
    )
    field_document_number = await session.cached_custom_field(
        DefaultCustomField.DOCUMENT_NUMBER
    )

    content = await session.retrieve_document(doc.id, original=True)

    result = await extract_with_vision(
        pdf_bytes=content,
        config=session.config,
    )

    if result is None:
        LOGGER.warning(
            "Vision extraction returned no result for '%s' (%d)", doc.title, doc.id
        )
        return None

    LOGGER.info("Vision result: %r", result)

    # Apply config aliases
    normalized_account_holders = tuple(
        session.config.lookup_account_holder(h) for h in result.account_holders
    )
    result = dataclasses.replace(result, account_holders=normalized_account_holders)

    if result.correspondent:
        result = dataclasses.replace(
            result,
            correspondent=session.config.lookup_correspondent(result.correspondent),
        )

    if result.document_type:
        result = dataclasses.replace(
            result,
            document_type=session.config.lookup_document_type(result.document_type),
        )

    # Update document fields
    if result.document_type:
        doc.title = result.document_type

    if result.date:
        doc.created = result.date.strftime("%Y-%m-%d")

    if execute:
        if result.correspondent:
            correspondent = await ensure_correspondent(session, result.correspondent)
            doc.correspondent = correspondent.id

        if result.document_type:
            document_type = await ensure_document_type(session, result.document_type)
            doc.document_type = document_type.id

    # Custom fields — same conservative overwrite logic as identify.py
    overwrite_fields = {field_account_holder.id}
    if result.account_number is not None:
        overwrite_fields.add(field_account_number.id)
    if result.document_number is not None:
        overwrite_fields.add(field_document_number.id)

    doc.custom_field_values = [
        cfv for cfv in doc.custom_field_values if cfv.field not in overwrite_fields
    ]

    if normalized_account_holders:
        doc.custom_field_values.append(
            CustomFieldValue(
                field=field_account_holder.id,
                value=", ".join(normalized_account_holders),
            )
        )

    if result.account_number:
        doc.custom_field_values.append(
            CustomFieldValue(field=field_account_number.id, value=result.account_number)
        )

    if result.document_number:
        doc.custom_field_values.append(
            CustomFieldValue(
                field=field_document_number.id, value=result.document_number
            )
        )
        doc.title = f"{doc.title} - {result.document_number}"

    # Tag as identified
    if identified_tag_name := session.config.predefined_tags.get("identified"):
        identified_tag = await session.lookup_tag(identified_tag_name)
        if identified_tag.id not in doc.tags:
            doc.tags.append(identified_tag.id)

    return doc


async def export_training_example(
    *,
    session: PaperlessSession,
    doc: Document,
    examples_dir: Path,
) -> Path:
    """Export a document and its current Paperless metadata as a training example."""
    example_dir = examples_dir / str(doc.id)
    example_dir.mkdir(parents=True, exist_ok=True)

    # Download and render pages
    pdf_bytes = await session.retrieve_document(doc.id, original=True)
    images = pdf_to_images(pdf_bytes, max_pages=session.config.vision_pages_to_process)
    for i, img_bytes in enumerate(images):
        (example_dir / f"page_{i}.png").write_bytes(img_bytes)

    # Resolve correspondent and document_type names from IDs
    correspondent_name = None
    if doc.correspondent is not None:
        try:
            correspondent_obj = await session.lookup_correspondent_by_id(doc.correspondent)
            correspondent_name = correspondent_obj.name
        except Exception:
            LOGGER.warning("Could not resolve correspondent ID %d", doc.correspondent)

    document_type_name = None
    if doc.document_type is not None:
        try:
            doc_type_obj = await session.lookup_document_type_by_id(doc.document_type)
            document_type_name = doc_type_obj.name
        except Exception:
            LOGGER.warning("Could not resolve document type ID %d", doc.document_type)

    # Resolve custom field values
    field_account_holder = await session.cached_custom_field(
        DefaultCustomField.ACCOUNT_HOLDER
    )
    field_account_number = await session.cached_custom_field(
        DefaultCustomField.ACCOUNT_NUMBER
    )
    field_document_number = await session.cached_custom_field(
        DefaultCustomField.DOCUMENT_NUMBER
    )

    account_holders: list[str] = []
    account_number = None
    document_number = None

    for cfv in doc.custom_field_values:
        if cfv.field == field_account_holder.id:
            account_holders = [h.strip() for h in cfv.value.split(",") if h.strip()]
        elif cfv.field == field_account_number.id:
            account_number = cfv.value
        elif cfv.field == field_document_number.id:
            document_number = cfv.value

    expected = {
        "correspondent": correspondent_name,
        "document_type": document_type_name,
        "date": doc.created_date,
        "account_holders": account_holders,
        "account_number": account_number,
        "document_number": document_number,
    }

    (example_dir / "expected.json").write_text(
        json.dumps(expected, indent=2, ensure_ascii=False)
    )

    # Metadata provenance snapshot
    metadata_snapshot = {
        "paperless_id": doc.id,
        "title": doc.title,
        "exported_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "document": doc.to_json(),
    }
    (example_dir / "metadata.json").write_text(
        json.dumps(metadata_snapshot, indent=2, ensure_ascii=False)
    )

    LOGGER.info("Exported training example for document %d to %s", doc.id, example_dir)
    return example_dir
