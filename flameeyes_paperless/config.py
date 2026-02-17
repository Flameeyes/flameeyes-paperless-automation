# SPDX-FileCopyrightText: 2024 Diego Elio Pettenò
#
# SPDX-License-Identifier: MIT

import dataclasses
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, NotRequired, Self, TypedDict

CONFIG_FILE: Final[Path] = Path("./paperless-automation.toml")


class VisionConfig(TypedDict, total=False):
    ollama_url: str
    model: str
    examples_dir: str
    max_few_shot_examples: int
    pages_to_process: int


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

    vision: VisionConfig = dataclasses.field(default_factory=dict)

    @property
    def vision_ollama_url(self) -> str:
        return self.vision.get("ollama_url", "http://localhost:11434")

    @property
    def vision_model(self) -> str:
        return self.vision.get("model", "qwen2.5vl:3b")

    @property
    def vision_examples_dir(self) -> Path:
        return Path(self.vision.get("examples_dir", "./vision-examples"))

    @property
    def vision_max_few_shot_examples(self) -> int:
        return int(self.vision.get("max_few_shot_examples", 3))

    @property
    def vision_pages_to_process(self) -> int:
        return int(self.vision.get("pages_to_process", 2))

    @property
    def vision_timeout(self) -> float:
        """Timeout in seconds for VLM requests. 0 means no timeout."""
        return float(self.vision.get("timeout", 0))

    def lookup_account_holder(self, account_holder: str) -> str:
        return self.aliases.get("account_holder", {}).get(
            account_holder, account_holder
        )

    def lookup_correspondent(self, correspondent: str) -> str:
        return self.aliases.get("correspondent", {}).get(correspondent, correspondent)

    def lookup_document_type(self, document_type: str) -> str:
        return self.aliases.get("document_type", {}).get(document_type, document_type)

    @classmethod
    def from_file(cls) -> Self:
        toml_config = tomllib.load(CONFIG_FILE.open("rb"))

        return cls(**toml_config)
