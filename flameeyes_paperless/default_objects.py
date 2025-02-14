# SPDX-FileCopyrightText: 2024 Diego Elio Pettenò
#
# SPDX-License-Identifier: MIT

from enum import StrEnum


class DefaultCustomField(StrEnum):
    ACCOUNT_HOLDER = "Account Holder"
    ACCOUNT_NUMBER = "Account Number"
    DOCUMENT_NUMBER = "Document Number"
