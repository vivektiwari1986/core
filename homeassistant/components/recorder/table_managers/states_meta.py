"""Support managing StatesMeta."""
from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, cast

from lru import LRU  # pylint: disable=no-name-in-module
from sqlalchemy.orm.session import Session

from homeassistant.core import Event

from . import BaseTableManager
from ..const import SQLITE_MAX_BIND_VARS
from ..db_schema import StatesMeta
from ..queries import find_all_states_metadata_ids, find_states_metadata_ids
from ..util import chunked

if TYPE_CHECKING:
    from ..core import Recorder

CACHE_SIZE = 8192


class StatesMetaManager(BaseTableManager):
    """Manage the StatesMeta table."""

    def __init__(self, recorder: Recorder) -> None:
        """Initialize the states meta manager."""
        self._id_map: dict[str, int] = LRU(CACHE_SIZE)
        self._pending: dict[str, StatesMeta] = {}
        self._did_first_load = False
        super().__init__(recorder)

    def load(self, events: list[Event], session: Session) -> None:
        """Load the entity_id to metadata_id mapping into memory.

        This call is not thread-safe and must be called from the
        recorder thread.
        """
        self._did_first_load = True
        self.get_many(
            {
                event.data["new_state"].entity_id
                for event in events
                if event.data.get("new_state") is not None
            },
            session,
            True,
        )

    def get(self, entity_id: str, session: Session, from_recorder: bool) -> int | None:
        """Resolve entity_id to the metadata_id.

        This call is not thread-safe after startup since
        purge can remove all references to an entity_id.

        When calling this method from the recorder thread, set
        from_recorder to True to ensure any missing entity_ids
        are added to the cache.
        """
        return self.get_many((entity_id,), session, from_recorder)[entity_id]

    def get_metadata_id_to_entity_id(self, session: Session) -> dict[int, str]:
        """Resolve all entity_ids to metadata_ids.

        This call is always thread-safe.
        """
        with session.no_autoflush:
            return dict(tuple(session.execute(find_all_states_metadata_ids())))  # type: ignore[arg-type]

    def get_many(
        self, entity_ids: Iterable[str], session: Session, from_recorder: bool
    ) -> dict[str, int | None]:
        """Resolve entity_id to metadata_id.

        This call is not thread-safe after startup since
        purge can remove all references to an entity_id.

        When calling this method from the recorder thread, set
        from_recorder to True to ensure any missing entity_ids
        are added to the cache.
        """
        results: dict[str, int | None] = {}
        missing: list[str] = []
        for entity_id in entity_ids:
            if (metadata_id := self._id_map.get(entity_id)) is None:
                missing.append(entity_id)

            results[entity_id] = metadata_id

        if not missing:
            return results

        # Only update the cache if we are in the recorder thread
        # or the recorder event loop has not started yet since
        # there is a chance that we could have just deleted all
        # instances of an entity_id from the database via purge
        # and we do not want to add it back to the cache from another
        # thread (history query).
        update_cache = from_recorder or not self._did_first_load

        with session.no_autoflush:
            for missing_chunk in chunked(missing, SQLITE_MAX_BIND_VARS):
                for metadata_id, entity_id in session.execute(
                    find_states_metadata_ids(missing_chunk)
                ):
                    metadata_id = cast(int, metadata_id)
                    results[entity_id] = metadata_id

                    if update_cache:
                        self._id_map[entity_id] = metadata_id

        return results

    def get_pending(self, entity_id: str) -> StatesMeta | None:
        """Get pending StatesMeta that have not be assigned ids yet.

        This call is not thread-safe and must be called from the
        recorder thread.
        """
        return self._pending.get(entity_id)

    def add_pending(self, db_states_meta: StatesMeta) -> None:
        """Add a pending StatesMeta that will be committed at the next interval.

        This call is not thread-safe and must be called from the
        recorder thread.
        """
        assert db_states_meta.entity_id is not None
        entity_id: str = db_states_meta.entity_id
        self._pending[entity_id] = db_states_meta

    def post_commit_pending(self) -> None:
        """Call after commit to load the metadata_ids of the new StatesMeta into the LRU.

        This call is not thread-safe and must be called from the
        recorder thread.
        """
        for entity_id, db_states_meta in self._pending.items():
            self._id_map[entity_id] = db_states_meta.metadata_id
        self._pending.clear()

    def reset(self) -> None:
        """Reset the states meta manager after the database has been reset or changed.

        This call is not thread-safe and must be called from the
        recorder thread.
        """
        self._id_map.clear()
        self._pending.clear()

    def evict_purged(self, entity_ids: Iterable[str]) -> None:
        """Evict purged event_types from the cache when they are no longer used.

        This call is not thread-safe and must be called from the
        recorder thread.
        """
        for entity_id in entity_ids:
            self._id_map.pop(entity_id, None)
