# SPDX-FileCopyrightText: 2024 Diego Elio Pettenò
#
# SPDX-License-Identifier: MIT

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any, Final, Self, TypedDict

from more_itertools import only


@dataclasses.dataclass(slots=True, kw_only=True)
class User:
    id: Final[int]
    username: str
    email: str
    password: str
    first_name: str
    last_name: str
    date_joined: str
    is_staff: bool
    is_active: bool
    is_superuser: bool
    is_mfa_enabled: bool
    groups: list[int]
    user_permissions: list[str]
    inherited_permissions: Final[Sequence[str]]


@dataclasses.dataclass(slots=True, kw_only=True)
class Group:
    id: Final[int]
    name: str
    permissions: list[str]


@dataclasses.dataclass(slots=True, kw_only=True)
class UsersAndGroups:
    users: set[int]
    groups: set[int]


@dataclasses.dataclass(slots=True, kw_only=True)
class Permission:
    view: UsersAndGroups
    change: UsersAndGroups

    @classmethod
    def from_json(cls, json_dict: Mapping[str, Any]) -> Self:
        return cls(
            view=UsersAndGroups(
                users=set(json_dict["view"]["users"]),
                groups=set(json_dict["view"]["groups"]),
            ),
            change=UsersAndGroups(
                users=set(json_dict["change"]["users"]),
                groups=set(json_dict["change"]["groups"]),
            ),
        )

    def to_json(self) -> Mapping[str, Any]:
        return {
            "view": {
                "users": sorted(self.view.users),
                "groups": sorted(self.view.groups),
            },
            "change": {
                "users": sorted(self.change.users),
                "groups": sorted(self.change.groups),
            },
        }


@dataclasses.dataclass(slots=True, kw_only=True)
class Tag:
    id: Final[int]
    slug: str
    name: str
    color: str
    text_color: Final[str]
    match: str
    matching_algorithm: int
    is_insensitive: bool
    is_inbox_tag: bool
    document_count: Final[int]
    parent: int | None
    children: Final[Sequence[int]]
    owner: int | None
    user_can_change: dataclasses.InitVar[bool | None] = None
    permissions: dataclasses.InitVar[Mapping[str, Any] | None] = None

    actual_permissions: Permission | None = None

    def __post_init__(
        self, user_can_change: bool | None, permissions: Mapping[str, Any] | None
    ) -> None:
        if permissions:
            self.actual_permissions = Permission.from_json(permissions)

    def to_json(self) -> Mapping[str, Any]:
        tag_dict = dataclasses.asdict(self)
        permissions = tag_dict.pop("actual_permissions")
        if self.actual_permissions:
            tag_dict["set_permissions"] = permissions.to_json()

        return tag_dict


@dataclasses.dataclass(slots=True, kw_only=True)
class Correspondent:
    id: Final[int]
    slug: str
    name: str
    match: str
    matching_algorithm: int
    is_insensitive: bool
    document_count: Final[int]
    owner: int | None
    user_can_change: dataclasses.InitVar[bool | None] = None
    permissions: dataclasses.InitVar[Mapping[str, Any] | None] = None

    actual_permissions: Permission | None = None

    def __post_init__(
        self, user_can_change: bool | None, permissions: Mapping[str, Any] | None
    ) -> None:
        if permissions:
            self.actual_permissions = Permission.from_json(permissions)

    def to_json(self) -> Mapping[str, Any]:
        correspondent_dict = dataclasses.asdict(self)
        permissions = correspondent_dict.pop("actual_permissions")
        if self.actual_permissions:
            correspondent_dict["set_permissions"] = permissions.to_json()

        return correspondent_dict


@dataclasses.dataclass(slots=True, kw_only=True)
class DocumentType:
    id: Final[int]
    slug: str
    name: str
    match: str
    matching_algorithm: int
    is_insensitive: bool
    document_count: Final[int]
    owner: int | None
    user_can_change: dataclasses.InitVar[bool | None] = None
    permissions: dataclasses.InitVar[Mapping[str, Any] | None] = None

    actual_permissions: Permission | None = None

    def __post_init__(
        self, user_can_change: bool | None, permissions: Mapping[str, Any] | None
    ) -> None:
        if permissions:
            self.actual_permissions = Permission.from_json(permissions)

    def to_json(self) -> Mapping[str, Any]:
        document_type_dict = dataclasses.asdict(self)
        permissions = document_type_dict.pop("actual_permissions")
        if self.actual_permissions:
            document_type_dict["set_permissions"] = permissions.to_json()

        return document_type_dict


@dataclasses.dataclass(slots=True, kw_only=True)
class StoragePath:
    id: Final[int]
    slug: str
    name: str
    path: str
    match: str
    matching_algorithm: int
    is_insensitive: bool
    document_count: Final[int]
    owner: int | None
    user_can_change: dataclasses.InitVar[bool | None] = None
    permissions: dataclasses.InitVar[Mapping[str, Any] | None] = None

    actual_permissions: Permission | None = None

    def __post_init__(
        self, user_can_change: bool | None, permissions: Mapping[str, Any] | None
    ) -> None:
        if permissions:
            self.actual_permissions = Permission.from_json(permissions)

    def to_json(self) -> Mapping[str, Any]:
        obj_dict = dataclasses.asdict(self)
        permissions = obj_dict.pop("actual_permissions")
        if self.actual_permissions:
            obj_dict["set_permissions"] = permissions.to_json()

        return obj_dict


@dataclasses.dataclass(slots=True, kw_only=True)
class CustomFieldExtraData:
    select_options: list[None | str] = dataclasses.field(default_factory=list)
    default_currency: None | str = None


@dataclasses.dataclass(slots=True, kw_only=True)
class CustomField:
    id: Final[int]
    name: str
    data_type: Final[str]
    extra_data: dataclasses.InitVar[Mapping[str, Any]]
    document_count: Final[int]
    extras: CustomFieldExtraData = dataclasses.field(init=False)

    def __post_init__(self, extra_data: Mapping[str, Any] | None) -> None:
        if extra_data is not None:
            self.extras = CustomFieldExtraData(**extra_data)
        else:
            self.extras = CustomFieldExtraData()

    def to_json(self) -> Mapping[str, Any]:
        custom_field_dict = dataclasses.asdict(self)
        extra_data = custom_field_dict.pop("extras")
        custom_field_dict["extra_data"] = extra_data

        return custom_field_dict


@dataclasses.dataclass(slots=True, kw_only=True)
class CustomFieldValue:
    field: int
    value: str


@dataclasses.dataclass(slots=True, kw_only=True)
class Document:
    id: Final[int]
    correspondent: int | None
    document_type: int | None
    storage_path: int | None
    title: str
    content: Final[str]
    tags: list[int]
    created: str
    created_date: str
    modified: str
    added: str
    deleted_at: str | None
    archive_serial_number: str | None
    original_file_name: Final[str]
    archived_file_name: Final[str]
    owner: int | None
    is_shared_by_requester: Final[bool]
    notes: list[str]
    page_count: Final[int]
    mime_type: str

    custom_fields: dataclasses.InitVar[Sequence[Mapping[str, Any]]]
    custom_field_values: list[CustomFieldValue] = dataclasses.field(init=False)

    user_can_change: dataclasses.InitVar[bool | None] = None
    permissions: dataclasses.InitVar[Mapping[str, Any] | None] = None

    actual_permissions: Permission | None = None

    def __post_init__(
        self,
        custom_fields: Sequence[Mapping[str, Any]],
        user_can_change: bool | None,
        permissions: Mapping[str, Any] | None,
    ) -> None:
        if permissions:
            self.actual_permissions = Permission.from_json(permissions)

        self.custom_field_values = [
            CustomFieldValue(**custom_field_value)
            for custom_field_value in custom_fields
        ]

    def to_json(self) -> Mapping[str, Any]:
        document_dict = dataclasses.asdict(self)
        permissions = document_dict.pop("actual_permissions")
        if self.actual_permissions:
            document_dict["set_permissions"] = permissions.to_json()

        custom_fields = document_dict.pop("custom_field_values")
        document_dict["custom_fields"] = custom_fields

        return document_dict


class MetadataEntry(TypedDict):
    namespace: str
    prefix: str
    key: str
    value: str


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class DocumentMetadata:
    original_checksum: str
    original_size: int
    original_mime_type: str
    media_filename: str
    has_archive_version: bool
    original_metadata: Sequence[MetadataEntry]
    archive_checksum: str
    archive_media_filename: str
    original_filename: str
    archive_size: int
    archive_metadata: Sequence[MetadataEntry]
    lang: str

    @property
    def original_producer(self) -> str | None:
        producer_metadata = only(
            entry for entry in self.original_metadata if entry["key"] == "Producer"
        )
        if producer_metadata:
            return producer_metadata["value"]

        return None
