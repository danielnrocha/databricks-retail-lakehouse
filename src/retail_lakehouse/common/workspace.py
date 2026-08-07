"""Shared workspace helpers.

Exists because the same three lines were being written in five modules, and each copy narrowed the
SDK's optional types slightly differently — which is how one of them ends up being the copy that
does not check.
"""

from __future__ import annotations

from typing import Any

from databricks.sdk import WorkspaceClient


def warehouse_id(client: WorkspaceClient) -> str:
    """The SQL warehouse to run against, narrowed to a non-optional id.

    Free Edition allows exactly one warehouse, so "the first" is unambiguous here. On a paid tier
    this should be an explicit parameter — silently picking a warehouse is how work ends up billed
    to the wrong team.
    """
    warehouses = list(client.warehouses.list())
    if not warehouses:
        raise RuntimeError("No SQL warehouse available in this workspace.")
    chosen = warehouses[0].id
    if chosen is None:
        raise RuntimeError("The SQL warehouse has no id.")
    return chosen


def openai_client(client: WorkspaceClient) -> Any:
    """An OpenAI-compatible client bound to this workspace's serving endpoints.

    Typed as Any deliberately: the SDK returns an untyped client, and pretending otherwise with a
    fabricated protocol would assert a shape nobody verified.
    """
    return client.serving_endpoints.get_open_ai_client()  # type: ignore[no-untyped-call]
