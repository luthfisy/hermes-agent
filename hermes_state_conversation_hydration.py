"""Bounded authorization-aware hydration for derived conversation indexes."""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

from conversation_index import (
    HydratedMessage,
    MessageIndexState,
    MessageReference,
    canonical_content_hash,
    canonical_hydration_text,
    canonical_message_index_state,
)


class SessionConversationHydrationMixin:
    """Validate untrusted stable references against canonical rows before returning text."""

    _INDEX_HYDRATE_LIMIT = 256

    def hydrate_message_references(
        self,
        references: Sequence[MessageReference],
        *,
        authorized_conversation_ids: Optional[Iterable[str]] = None,
        include_compacted: bool = True,
        include_inactive: bool = False,
        limit: int = _INDEX_HYDRATE_LIMIT,
    ) -> tuple[HydratedMessage, ...]:
        self._validate_index_limit(limit, self._INDEX_HYDRATE_LIMIT)
        refs = tuple(references)
        if len(refs) > limit:
            raise ValueError(f"reference count exceeds hydration limit {limit}")
        refs = tuple(ref for ref in refs if isinstance(ref, MessageReference))
        if not refs:
            return ()

        allowed = self._normalize_index_conversation_ids(authorized_conversation_ids)
        allowed_set = set(allowed) if allowed is not None else None
        message_ids = tuple(sorted({ref.message_id for ref in refs}))

        def _read(conn):
            placeholders = ",".join("?" for _ in message_ids)
            rows = {
                int(row["id"]): row
                for row in conn.execute(
                    "SELECT id, session_id, typeof(content) AS storage_type, "
                    "CAST(content AS BLOB) AS content_bytes, content, active, compacted, role, timestamp "
                    f"FROM messages WHERE id IN ({placeholders})",
                    message_ids,
                ).fetchall()
            }

            hydrated = []
            for ref in refs:
                if allowed_set is not None and ref.conversation_id not in allowed_set:
                    continue
                row = rows.get(ref.message_id)
                if row is None or row["session_id"] != ref.conversation_id:
                    continue
                state = canonical_message_index_state(row["active"], row["compacted"])
                if not include_inactive and state is MessageIndexState.INACTIVE:
                    continue
                if not include_inactive and not include_compacted and state is MessageIndexState.COMPACTED:
                    continue
                if canonical_content_hash(row["storage_type"], row["content_bytes"]) != ref.content_hash:
                    continue
                text = canonical_hydration_text(self._decode_content(row["content"]))
                if ref.end > len(text):
                    continue
                hydrated.append(HydratedMessage(
                    reference=ref, state=state, text=text[ref.start:ref.end],
                    role=row["role"], timestamp=float(row["timestamp"]),
                ))
            return tuple(hydrated)

        return self._read_index_snapshot(_read)
