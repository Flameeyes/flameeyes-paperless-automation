# SPDX-FileCopyrightText: 2024 Diego Elio Pettenò
#
# SPDX-License-Identifier: MIT

import contextlib
from collections.abc import AsyncGenerator, AsyncIterator, Collection, Iterator, Mapping
from enum import StrEnum
from functools import cached_property
from typing import Any, Final, Self
from urllib.parse import urljoin, urlparse, urlunparse

import aiohttp
from aiohttp import BasicAuth, ClientSession

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


class PaperlessSession(contextlib.AbstractAsyncContextManager):
    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config: Final[Config] = config
        self._http_session: None | ClientSession = None
        # manual cache for async cached custom fields
        self._cached_custom_fields: dict[DefaultCustomField, CustomField] = {}

    @cached_property
    def http_auth(self) -> BasicAuth:
        return BasicAuth(self.config.username, self.config.password)

    async def default_access_group(self) -> Group:
        async for group in self.groups():
            if group.name == self.config.all_access_group:
                return group

        raise ObjectNotFound(
            f"No group found matching '{self.config.all_access_group}'"
        )

    async def default_permissions(self) -> Permission:
        all_access_group = await self.default_access_group()

        return Permission(
            view=UsersAndGroups(users=set(), groups={all_access_group.id}),
            change=UsersAndGroups(users=set(), groups={all_access_group.id}),
        )

    @cached_property
    def _api_version_headers(self) -> Mapping[str, str]:
        return {"accept": f"application/json; version={self._api_version}"}

    async def __aenter__(self) -> Self:
        self._http_session = aiohttp.ClientSession(auth=self.http_auth)
        # Find the API version.
        resp = await self._http_session.get(f"{self.config.url}/api/")
        resp.raise_for_status()
        self._api_version = min(9, int(resp.headers.get("X-Api-Version", "1")))

        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        if s := self._http_session:
            await s.close()

    @cached_property
    def _api_path(self) -> str:
        return urljoin(self.config.url, "api/")

    def _normalize_path(self, path: str) -> str:
        path = urljoin(self.config.url, path)
        if not path.startswith(self._api_path):
            raise ValueError(f"Invalid Paperless API path {path}")

        return path

    async def _get(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        if not (s := self._http_session):
            raise RuntimeError("Session not opened!")
        resp = await s.get(
            self._normalize_path(path), headers=self._api_version_headers, params=params
        )
        resp.raise_for_status()
        return await resp.json()

    async def _get_pdf(self, path: str, original: bool = True) -> bytes:
        if not (s := self._http_session):
            raise RuntimeError("Session not opened!")
        resp = await s.get(
            self._normalize_path(path),
            params={"original": "true" if original else "false"},
        )
        resp.raise_for_status()
        return await resp.read()

    async def _patch(self, path: str, json: Mapping[str, Any]) -> dict[str, Any]:
        if not (s := self._http_session):
            raise RuntimeError("Session not opened!")
        resp = await s.patch(self._normalize_path(path), json=json)
        resp.raise_for_status()
        return await resp.json()

    async def _post(self, path: str, json: Mapping[str, Any]) -> dict[str, Any]:
        if not (s := self._http_session):
            raise RuntimeError("Session not opened!")
        resp = await s.post(self._normalize_path(path), json=json)
        resp.raise_for_status()
        return await resp.json()

    @staticmethod
    def _filter_fields(cls: type, data: Mapping[str, Any]) -> dict[str, Any]:
        """Filter a JSON dict to only fields accepted by the given dataclass.

        Uses __dataclass_fields__ instead of dataclasses.fields() to also
        include InitVar fields, which are accepted by __init__ but not
        returned by dataclasses.fields().
        """
        known = cls.__dataclass_fields__.keys()  # type: ignore[attr-defined]
        return {k: v for k, v in data.items() if k in known}

    def _extract_objects(
        self, object_type: ObjectType, resp_json: dict[str, Any]
    ) -> Iterator[Any]:
        cls = _TYPE_TO_STRUCTURE[object_type]
        return (cls(**self._filter_fields(cls, obj)) for obj in resp_json["results"])

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

    async def _get_objects(
        self,
        object_type: ObjectType,
        full_permissions: bool = False,
        order_fields: str | None = None,
        **kwargs: str,
    ) -> AsyncIterator[Any]:
        starting_path = f"/api/{object_type}/"
        params = {**kwargs}
        if full_permissions:
            params["full_perms"] = "true"

        if order_fields:
            params["ordering"] = order_fields

        resp_json = await self._get(starting_path, params)
        for obj in self._extract_objects(object_type, resp_json):
            yield obj

        while next_url := self._fix_next_url(resp_json.get("next")):
            resp_json = await self._get(next_url, {})
            for obj in self._extract_objects(object_type, resp_json):
                yield obj

    async def users(self) -> AsyncIterator[User]:
        async for u in self._get_objects(ObjectType.USER):
            yield u

    async def groups(self) -> AsyncIterator[Group]:
        async for g in self._get_objects(ObjectType.GROUP):
            yield g

    async def tags(self, full_permissions: bool = False) -> AsyncIterator[Tag]:
        async for t in self._get_objects(
            ObjectType.TAG, full_permissions=full_permissions
        ):
            yield t

    async def lookup_tag(self, name: str) -> Tag:
        name_lower = name.lower()
        async for obj in self.tags():
            if name_lower == obj.name.lower():
                return obj

        raise ObjectNotFound(f"No tag found matching '{name}'")

    async def update_tag(self, tag: Tag) -> dict[str, Any]:
        tag_json = tag.to_json()
        return await self._patch(f"/api/tags/{tag.id}/", json=tag_json)

    async def new_tag(
        self,
        name: str,
        slug: str,
        matching_algorithm: int = 0,
        is_inbox_tag: bool = False,
    ) -> dict[str, Any]:
        return await self._post(
            "/api/tags/",
            json={
                "name": name,
                "slug": slug,
                "matching_algorithm": matching_algorithm,
                "is_inbox_tag": is_inbox_tag,
                "owner": None,
                "set_permissions": (await self.default_permissions()).to_json(),
            },
        )

    async def correspondents(
        self, full_permissions: bool = False
    ) -> AsyncIterator[Correspondent]:
        async for c in self._get_objects(
            ObjectType.CORRESPONDENT, full_permissions=full_permissions
        ):
            yield c

    async def lookup_correspondent(self, name: str) -> Correspondent:
        name_lower = name.lower()
        async for obj in self.correspondents():
            if name_lower == obj.name.lower():
                return obj

        raise ObjectNotFound(f"No correspondent found matching '{name}'")

    async def lookup_correspondent_by_id(self, correspondent_id: int) -> Correspondent:
        resp_json = await self._get(f"/api/correspondents/{correspondent_id}/", {})
        return Correspondent(**self._filter_fields(Correspondent, resp_json))

    async def update_correspondent(
        self, correspondent: Correspondent
    ) -> dict[str, Any]:
        correspondent_json = correspondent.to_json()

        return await self._patch(
            f"/api/correspondents/{correspondent.id}/", json=correspondent_json
        )

    async def new_correspondent(self, name: str, slug: str) -> dict[str, Any]:
        return await self._post(
            "/api/correspondents/",
            json={
                "name": name,
                "slug": slug,
                "owner": None,
                "set_permissions": (await self.default_permissions()).to_json(),
            },
        )

    async def document_types(
        self, full_permissions: bool = False
    ) -> AsyncIterator[DocumentType]:
        async for dt in self._get_objects(
            ObjectType.DOCUMENT_TYPE, full_permissions=full_permissions
        ):
            yield dt

    async def lookup_document_type(self, name: str) -> DocumentType:
        name_lower = name.lower()
        async for obj in self.document_types():
            if name_lower == obj.name.lower():
                return obj

        raise ObjectNotFound(f"No document type found matching '{name}'")

    async def lookup_document_type_by_id(self, document_type_id: int) -> DocumentType:
        resp_json = await self._get(f"/api/document_types/{document_type_id}/", {})
        return DocumentType(**self._filter_fields(DocumentType, resp_json))

    async def storage_paths(
        self, full_permissions: bool = False
    ) -> AsyncIterator[StoragePath]:
        async for sp in self._get_objects(
            ObjectType.STORAGE_PATH, full_permissions=full_permissions
        ):
            yield sp

    async def lookup_storage_path(self, name: str) -> StoragePath:
        name_lower = name.lower()
        async for obj in self.storage_paths():
            if name_lower == obj.name.lower():
                return obj

        raise ObjectNotFound(f"No storage path found matching '{name}'")

    async def update_document_type(self, document_type: DocumentType) -> dict[str, Any]:
        document_type_json = document_type.to_json()

        return await self._patch(
            f"/api/document_types/{document_type.id}/", json=document_type_json
        )

    async def new_document_type(self, name: str, slug: str) -> dict[str, Any]:
        return await self._post(
            "/api/document_types/",
            json={
                "name": name,
                "slug": slug,
                "owner": None,
                "set_permissions": (await self.default_permissions()).to_json(),
            },
        )

    async def custom_fields(self) -> AsyncIterator[CustomField]:
        async for cf in self._get_objects(ObjectType.CUSTOM_FIELD):
            yield cf

    async def lookup_custom_field(self, name: str) -> CustomField:
        name_lower = name.lower()
        async for obj in self.custom_fields():
            if name_lower == obj.name.lower():
                return obj

        raise ObjectNotFound(f"No custom field found matching '{name}'")

    async def cached_custom_field(self, field: DefaultCustomField) -> CustomField:
        if field in self._cached_custom_fields:
            return self._cached_custom_fields[field]

        cf = await self.lookup_custom_field(field)
        self._cached_custom_fields[field] = cf
        return cf

    async def new_custom_field(self, name: str, data_type: str) -> dict[str, Any]:
        return await self._post(
            "/api/custom_fields/", json={"name": name, "data_type": data_type}
        )

    def _document_query(
        self,
        mime_type: str | None,
        required_tags: None | Collection[Tag],
        excluded_tags: None | Collection[Tag],
    ) -> dict[str, str]:
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

        return filter

    async def documents(
        self,
        full_permissions: bool = False,
        mime_type: str | None = "application/pdf",
        required_tags: None | Collection[Tag] = None,
        excluded_tags: None | Collection[Tag] = None,
    ) -> AsyncGenerator[Document, None]:
        """Retrieve documents based on the required tags."""
        filter = self._document_query(mime_type, required_tags, excluded_tags)

        async for obj in self._get_objects(
            ObjectType.DOCUMENT,
            full_permissions=full_permissions,
            order_fields="id",
            **filter,
        ):
            yield obj

    async def search_documents(
        self,
        mime_type: str | None = "application/pdf",
        required_tags: None | Collection[Tag] = None,
        excluded_tags: None | Collection[Tag] = None,
    ) -> AsyncIterator[int]:
        starting_path = "/api/documents/"
        params = {
            "fields": "id",
            # We set the page size to 1, because we don't care about the
            # returned values, we care only of the "all" field returned.
            "page_size": "1",
            **self._document_query(mime_type, required_tags, excluded_tags),
        }

        resp_json = await self._get(starting_path, params)

        for v in sorted(resp_json["all"]):
            yield v

    async def lookup_document(self, document_id: int) -> Document:
        resp_json = await self._get(f"/api/documents/{document_id}/", {})
        return Document(**self._filter_fields(Document, resp_json))

    async def retrieve_document(
        self, document_id: int, original: bool = False
    ) -> bytes:
        return await self._get_pdf(
            f"/api/documents/{document_id}/download/", original=original
        )

    async def retrieve_document_metadata(self, document_id: int) -> DocumentMetadata:
        resp_json = await self._get(f"/api/documents/{document_id}/metadata/", {})
        return DocumentMetadata(**self._filter_fields(DocumentMetadata, resp_json))

    async def update_document(self, document: Document) -> dict[str, Any]:
        document_json = document.to_json()

        return await self._patch(f"/api/documents/{document.id}/", json=document_json)

    async def documents_by_correspondent(
        self, correspondent_id: int
    ) -> AsyncGenerator[Document, None]:
        async for doc in self._get_objects(
            ObjectType.DOCUMENT,
            correspondent__id=str(correspondent_id),
            order_fields="id",
        ):
            yield doc

    async def documents_by_tag_id(self, tag_id: int) -> AsyncGenerator[Document, None]:
        async for doc in self._get_objects(
            ObjectType.DOCUMENT,
            tags__id__in=str(tag_id),
            order_fields="id",
        ):
            yield doc

    async def documents_by_document_type(
        self, document_type_id: int
    ) -> AsyncGenerator[Document, None]:
        async for doc in self._get_objects(
            ObjectType.DOCUMENT,
            document_type__id=str(document_type_id),
            order_fields="id",
        ):
            yield doc

    async def _delete(self, path: str) -> None:
        if not (s := self._http_session):
            raise RuntimeError("Session not opened!")
        resp = await s.delete(self._normalize_path(path))
        resp.raise_for_status()

    async def lookup_tag_by_id(self, tag_id: int) -> Tag:
        resp_json = await self._get(f"/api/tags/{tag_id}/", {})
        return Tag(**self._filter_fields(Tag, resp_json))

    async def delete_correspondent(self, correspondent_id: int) -> None:
        await self._delete(f"/api/correspondents/{correspondent_id}/")

    async def delete_tag(self, tag_id: int) -> None:
        await self._delete(f"/api/tags/{tag_id}/")

    async def delete_document_type(self, document_type_id: int) -> None:
        await self._delete(f"/api/document_types/{document_type_id}/")
