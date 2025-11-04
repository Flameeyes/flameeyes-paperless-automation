# SPDX-FileCopyrightText: 2024 Diego Elio Pettenò
#
# SPDX-License-Identifier: MIT

import logging
import re
from typing import Final

from more_itertools import only

from .default_objects import DefaultCustomField
from .session import ObjectNotFound, PaperlessSession
from .types import Correspondent, CustomField, DocumentType

LOGGER: Final[logging.Logger] = logging.getLogger("flameeyes-paperless-automation")


def to_slug(name: str) -> str:
    slug = name.lower()
    return re.sub(r"\W", "-", slug)


async def lookup_account_custom_fields(
    s: PaperlessSession,
) -> tuple[CustomField, CustomField, CustomField]:
    all_custom_fields = []
    async for cf in s.custom_fields():
        all_custom_fields.append(cf)

    account_name_field = only(
        custom_field
        for custom_field in all_custom_fields
        if custom_field.name == DefaultCustomField.ACCOUNT_HOLDER
    )

    account_number_field = only(
        custom_field
        for custom_field in all_custom_fields
        if custom_field.name == DefaultCustomField.ACCOUNT_NUMBER
    )

    document_number_field = only(
        custom_field
        for custom_field in all_custom_fields
        if custom_field.name == DefaultCustomField.DOCUMENT_NUMBER
    )

    if any(
        not field
        for field in (account_name_field, account_number_field, document_number_field)
    ):
        raise ObjectNotFound(
            f"Unable to find custom fields '{DefaultCustomField.ACCOUNT_HOLDER}', '{DefaultCustomField.ACCOUNT_NUMBER}', or '{DefaultCustomField.DOCUMENT_NUMBER}'"
        )

    assert account_name_field
    assert account_number_field
    assert document_number_field

    return account_name_field, account_number_field, document_number_field


async def ensure_account_custom_fields(
    s: PaperlessSession,
) -> tuple[CustomField, CustomField, CustomField]:
    all_custom_fields = []
    async for cf in s.custom_fields():
        all_custom_fields.append(cf)

    account_name_field = only(
        custom_field
        for custom_field in all_custom_fields
        if custom_field.name == DefaultCustomField.ACCOUNT_HOLDER
    )

    account_number_field = only(
        custom_field
        for custom_field in all_custom_fields
        if custom_field.name == DefaultCustomField.ACCOUNT_NUMBER
    )

    document_number_field = only(
        custom_field
        for custom_field in all_custom_fields
        if custom_field.name == DefaultCustomField.DOCUMENT_NUMBER
    )

    if account_name_field is None:
        await s.new_custom_field(
            name=DefaultCustomField.ACCOUNT_HOLDER, data_type="string"
        )
    if account_number_field is None:
        await s.new_custom_field(
            name=DefaultCustomField.ACCOUNT_NUMBER, data_type="string"
        )
    if document_number_field is None:
        await s.new_custom_field(
            name=DefaultCustomField.DOCUMENT_NUMBER, data_type="string"
        )

    return await lookup_account_custom_fields(s)


async def ensure_document_type(
    s: PaperlessSession,
    document_type_name: str,
) -> DocumentType:
    try:
        return await s.lookup_document_type(document_type_name)
    except ObjectNotFound:
        await s.new_document_type(
            name=document_type_name, slug=to_slug(document_type_name)
        )
        return await ensure_document_type(s, document_type_name)


async def ensure_correspondent(
    s: PaperlessSession,
    correspondent_name: str,
) -> Correspondent:
    try:
        return await s.lookup_correspondent(correspondent_name)
    except ObjectNotFound:
        await s.new_correspondent(
            name=correspondent_name, slug=to_slug(correspondent_name)
        )
        return await ensure_correspondent(s, correspondent_name)
