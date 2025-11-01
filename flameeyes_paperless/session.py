# SPDX-FileCopyrightText: 2024 Diego Elio Pettenò
#
# SPDX-License-Identifier: MIT

import contextlib
from collections.abc import Collection, Iterator, Mapping, Sequence
from enum import StrEnum
from functools import cache, cached_property
from typing import Any, Final, Self
from urllib.parse import urljoin, urlparse, urlunparse

from more_itertools import one
from requests import Response, Session
from requests.auth import HTTPBasicAuth

from .config import Config
from .default_objects import DefaultCustomField
from .types import (
    Correspondent,
    CustomField,
    Document,
    DocumentMetadata,
    DocumentType,
    Group,
    Permission,
    StoragePath,
    Tag,
    User,
    UsersAndGroups,
)


class ObjectNotFound(LookupError):
    pass


class ObjectType(StrEnum):
    USER = "users"
    GROUP = "groups"
    TAG = "tags"
    CORRESPONDENT = "correspondents"
    DOCUMENT_TYPE = "document_types"
    STORAGE_PATH = "storage_paths"
    CUSTOM_FIELD = "custom_fields"
    DOCUMENT = "documents"


_TYPE_TO_STRUCTURE: Mapping[ObjectType, type] = {
    ObjectType.USER: User,
    ObjectType.GROUP: Group,
    ObjectType.TAG: Tag,
    ObjectType.CORRESPONDENT: Correspondent,
    ObjectType.DOCUMENT_TYPE: DocumentType,
    ObjectType.STORAGE_PATH: StoragePath,
    ObjectType.CUSTOM_FIELD: CustomField,
    ObjectType.DOCUMENT: Document,
}


class PaperlessSession(contextlib.AbstractContextManager):
    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config: Final[Config] = config
        self._http_session: None | Session = None

    @cached_property
    def http_auth(self) -> HTTPBasicAuth:
        return HTTPBasicAuth(self.config.username, self.config.password)

    @cached_property
    def default_access_group(self) -> Group:
        return one(
            (
                group
                for group in self.groups()
                if group.name == self.config.all_access_group
            ),
            too_short=ObjectNotFound(
                f"No group found matching '{self.config.all_access_group}'"
            ),
        )

    @cached_property
    def default_permissions(self) -> Permission:
        all_access_group = self.default_access_group

        return Permission(
            view=UsersAndGroups(users=set(), groups={all_access_group.id}),
            change=UsersAndGroups(users=set(), groups={all_access_group.id}),
        )

    @cached_property
    def _api_version_headers(self) -> Mapping[str, str]:
        return {"accept": f"application/json; version={self._api_version}"}

    def __enter__(self) -> Self:
        self._http_session = Session()
        self._http_session.auth = self.http_auth
        # Find the API version.
        resp = self._http_session.get(f"{self.config.url}/api/")
        resp.raise_for_status()
        self._api_version = min(4, int(resp.headers["X-Api-Version"]))

        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if s := self._http_session:
            s.close()

    @cached_property
    def _api_path(self) -> str:
        return urljoin(self.config.url, "api/")

    def _normalize_path(self, path: str) -> str:
        path = urljoin(self.config.url, path)
        if not path.startswith(self._api_path):
            raise ValueError(f"Invalid Paperless API path {path}")

        return path

    def _get(self, path: str, params: Mapping[str, str]) -> Response:
        if not (s := self._http_session):
            raise RuntimeError("Session not opened!")
        resp = s.get(
            self._normalize_path(path), headers=self._api_version_headers, params=params
        )
        resp.raise_for_status()
        return resp

    def _get_pdf(self, path: str, original: bool = True) -> Response:
        if not (s := self._http_session):
            raise RuntimeError("Session not opened!")
        resp = s.get(
            self._normalize_path(path),
            params={"original": "true" if original else False},
        )
        resp.raise_for_status()
        return resp

    def _patch(self, path: str, json: Mapping[str, Any]) -> Response:
        if not (s := self._http_session):
            raise RuntimeError("Session not opened!")
        resp = s.patch(self._normalize_path(path), json=json)
        resp.raise_for_status()
        return resp

    def _post(self, path: str, json: Mapping[str, Any]) -> Response:
        if not (s := self._http_session):
            raise RuntimeError("Session not opened!")
        resp = s.post(self._normalize_path(path), json=json)
        resp.raise_for_status()
        return resp

    def _extract_objects(
        self, object_type: ObjectType, resp: Response
    ) -> Iterator[object]:
        return (
            _TYPE_TO_STRUCTURE[object_type](**obj) for obj in resp.json()["results"]
        )

    @staticmethod
    def _fix_next_url(next_url: str | None) -> str | None:
        if not next_url:
            return None

        # We don't need to pass params to the continuation fetch, as they will
        # be encoded by Paperless server. But we do need to parse the URL and
        # drop the host, because the returned response use an absolute URL with
        # HTTP even when fetched over HTTPS (!)
        received_url = urlparse(next_url)
        return urlunparse(
            (
                "",  # scheme
                "",  # netloc (hostname)
                received_url.path,
                received_url.params,
                received_url.query,
                received_url.fragment,
            )
        )

    def _get_objects(
        self,
        object_type: ObjectType,
        full_permissions: bool = False,
        order_fields: str | None = None,
        **kwargs: str,
    ) -> Iterator[object]:
        starting_path = f"/api/{object_type}/"
        params = {**kwargs}
        if full_permissions:
            params["full_perms"] = "true"

        if order_fields:
            params["ordering"] = order_fields

        resp = self._get(starting_path, params)
        yield from self._extract_objects(object_type, resp)
        while next_url := self._fix_next_url(resp.json()["next"]):
            resp = self._get(next_url, {})
            yield from self._extract_objects(object_type, resp)

    def users(self) -> Iterator[User]:
        return self._get_objects(ObjectType.USER)

    def groups(self) -> Iterator[Group]:
        return self._get_objects(ObjectType.GROUP)

    def tags(self, full_permissions: bool = False) -> Iterator[Tag]:
        return self._get_objects(ObjectType.TAG, full_permissions=full_permissions)

    def lookup_tag(self, name: str) -> Tag:
        name_lower = name.lower()
        return one(
            (obj for obj in self.tags() if name_lower == obj.name.lower()),
            too_short=ObjectNotFound(f"No tag found matching '{name}'"),
        )

    def update_tag(self, tag: Tag) -> Response:
        tag_json = tag.to_json()
        return self._patch(f"/api/tags/{tag.id}/", json=tag_json)

    def new_tag(
        self,
        name: str,
        slug: str,
        matching_algorithm: int = 0,
        is_inbox_tag: bool = False,
    ) -> Response:
        return self._post(
            "/api/tags/",
            json={
                "name": name,
                "slug": slug,
                "matching_algorithm": matching_algorithm,
                "is_inbox_tag": is_inbox_tag,
                "owner": None,
                "set_permissions": self.default_permissions.to_json(),
            },
        )

    def correspondents(self, full_permissions: bool = False) -> Iterator[Correspondent]:
        return self._get_objects(
            ObjectType.CORRESPONDENT, full_permissions=full_permissions
        )

    def lookup_correspondent(self, name: str) -> Correspondent:
        name_lower = name.lower()
        return one(
            (obj for obj in self.correspondents() if name_lower == obj.name.lower()),
            too_short=ObjectNotFound(f"No correspondent found matching '{name}'"),
        )

    def update_correspondent(self, correspondent: Correspondent) -> Response:
        correspondent_json = correspondent.to_json()

        return self._patch(
            f"/api/correspondents/{correspondent.id}/", json=correspondent_json
        )

    def new_correspondent(self, name: str, slug: str) -> Response:
        return self._post(
            "/api/correspondents/",
            json={
                "name": name,
                "slug": slug,
                "owner": None,
                "set_permissions": self.default_permissions.to_json(),
            },
        )

    def document_types(self, full_permissions: bool = False) -> Iterator[DocumentType]:
        return self._get_objects(
            ObjectType.DOCUMENT_TYPE, full_permissions=full_permissions
        )

    def lookup_document_type(self, name: str) -> DocumentType:
        name_lower = name.lower()
        return one(
            (obj for obj in self.document_types() if name_lower == obj.name.lower()),
            too_short=ObjectNotFound(f"No document type found matching '{name}'"),
        )

    def storage_paths(self, full_permissions: bool = False) -> Iterator[StoragePath]:
        return self._get_objects(
            ObjectType.STORAGE_PATH, full_permissions=full_permissions
        )

    def lookup_storage_path(self, name: str) -> DocumentType:
        name_lower = name.lower()
        return one(
            (obj for obj in self.storage_paths() if name_lower == obj.name.lower()),
            too_short=ObjectNotFound(f"No storage path found matching '{name}'"),
        )

    def update_document_type(self, document_type: DocumentType) -> Response:
        document_type_json = document_type.to_json()

        return self._patch(
            f"/api/document_types/{document_type.id}/", json=document_type_json
        )

    def new_document_type(self, name: str, slug: str) -> Response:
        return self._post(
            "/api/document_types/",
            json={
                "name": name,
                "slug": slug,
                "owner": None,
                "set_permissions": self.default_permissions.to_json(),
            },
        )

    def custom_fields(self) -> Sequence[CustomField]:
        return self._get_objects(ObjectType.CUSTOM_FIELD)

    def lookup_custom_field(self, name: str) -> CustomField:
        name_lower = name.lower()
        return one(
            (obj for obj in self.custom_fields() if name_lower == obj.name.lower()),
            too_short=ObjectNotFound(f"No custom field found matching '{name}'"),
        )

    @cache
    def cached_custom_field(self, field: DefaultCustomField) -> CustomField:
        return self.lookup_custom_field(field)

    def new_custom_field(self, name: str, data_type: str) -> Response:
        return self._post(
            "/api/custom_fields/", json={"name": name, "data_type": data_type}
        )

    def documents(
        self,
        full_permissions: bool = False,
        mime_type: str | None = "application/pdf",
        required_tags: None | Collection[Tag] = None,
        excluded_tags: None | Collection[Tag] = None,
    ) -> Iterator[Document]:
        """Search (list) documents based on the required tags."""
        filter = {}
        if mime_type is not None:
            filter["mime_type"] = mime_type
        if required_tags is not None:
            filter["tags__id__in"] = ",".join(
                str(id) for id in sorted(tag.id for tag in required_tags)
            )
        if excluded_tags is not None:
            filter["tags__id__none"] = ",".join(
                str(id) for id in sorted(tag.id for tag in excluded_tags)
            )

        return self._get_objects(
            ObjectType.DOCUMENT,
            full_permissions=full_permissions,
            order_fields="id",
            **filter,
        )

    def lookup_document(self, document_id: int) -> Document:
        resp = self._get(f"/api/documents/{document_id}/", {})
        return Document(**resp.json())

    def retrieve_document(self, document_id: int, original: bool = False) -> bytes:
        return self._get_pdf(
            f"/api/documents/{document_id}/download/", original=original
        ).content

    def retrieve_document_metadata(self, document_id: int) -> DocumentMetadata:
        resp = self._get(f"/api/documents/{document_id}/metadata/", {})
        return DocumentMetadata(**resp.json())

    def update_document(self, document: Document) -> Response:
        document_json = document.to_json()

        return self._patch(f"/api/documents/{document.id}/", json=document_json)
