# SPDX-FileCopyrightText: 2024 Diego Elio Pettenò
#
# SPDX-License-Identifier: MIT

import dataclasses
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, NotRequired, Self, TypedDict

CONFIG_FILE: Final[Path] = Path("./paperless-automation.toml")


class Aliases(TypedDict):
    account_holder: NotRequired[Mapping[str, str]]
    correspondent: NotRequired[Mapping[str, str]]


class PredefinedTags(TypedDict, total=True):
    identified: NotRequired[str]
    inbox: NotRequired[str]
    scanned: NotRequired[str]


class PredefinedStoragePaths(TypedDict, total=True):
    unsorted: NotRequired[str]
    scanned: NotRequired[str]


@dataclasses.dataclass(frozen=True, kw_only=True, slots=True, eq=False)
class Config:
    url: str
    username: str
    password: str

    object_owner: str
    all_access_group: str

    scan_software: Sequence[str] = ()

    predefined_tags: PredefinedTags
    predefined_storage_paths: PredefinedStoragePaths

    aliases: Aliases

    def lookup_account_holder(self, account_holder: str) -> str:
        return self.aliases.get("account_holder", {}).get(
            account_holder, account_holder
        )

    def lookup_correspondent(self, correspondent: str) -> str:
        return self.aliases.get("correspondent", {}).get(correspondent, correspondent)

    @classmethod
    def from_file(cls) -> Self:
        toml_config = tomllib.load(CONFIG_FILE.open("rb"))

        return cls(**toml_config)
