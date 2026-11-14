"""Read-only host display seam; authoritative IDs never come from labels."""
from __future__ import annotations

from typing import Mapping, Protocol, Sequence


class DisplayAdapter(Protocol):
    """Read trusted core records on the already selected business connection.

    Decoration must retain IDs and historical supplier snapshots. It is display
    data only and never changes the persisted binding or a command's identity.
    ``order_labels`` maps permanent order references to order_number,
    customer_name and ship_to_address; unavailable values may be omitted.
    """

    def decorate_references(self, conn, references: list[dict]) -> list[dict]: ...

    def order_labels(self, conn, order_refs: Sequence[str]) -> Mapping[str, dict]: ...


class SnapshotDisplayAdapter:
    """Safe fallback when the host has no current display record available."""

    def decorate_references(self, conn, references: list[dict]) -> list[dict]:
        return [dict(reference, snapshot=dict(reference.get('snapshot') or {}))
                for reference in references]

    def order_labels(self, conn, order_refs: Sequence[str]) -> Mapping[str, dict]:
        return {}


SNAPSHOT_DISPLAY = SnapshotDisplayAdapter()
