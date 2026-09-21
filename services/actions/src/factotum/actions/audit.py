"""Reading the audit trail: who did what, through which device, and on whose behalf.

The trail is the tenant's accountability record, so it is read by the tenant's administrators, and
never through a device (ADR 0004). What each person did is theirs to see in the web app (slice 9).
"""

from datetime import datetime

from factotum.actions.store import ActionsStore
from factotum.contracts.actions import AuditList
from factotum.contracts.errors import PolicyViolationError
from factotum.contracts.identity import Principal, Role
from factotum.contracts.ids import DeviceId

MAX_PAGE = 200


class AuditReader:
    def __init__(self, store: ActionsStore) -> None:
        self._store = store

    async def list(
        self,
        principal: Principal,
        *,
        before: datetime | None = None,
        limit: int = 50,
        skill_name: str | None = None,
        device_id: DeviceId | None = None,
    ) -> AuditList:
        """The latest records first, ``limit`` at a time (at most ``MAX_PAGE``). The page says
        where the next one begins when there is one."""
        if principal.device_id is not None:
            raise PolicyViolationError(
                "the audit trail is read from the hub, never through a device"
            )
        if Role.ADMIN not in principal.roles:
            raise PolicyViolationError("the audit trail is read by the administrators")
        limit = max(1, min(limit, MAX_PAGE))
        found = await self._store.list_audit(
            principal.tenant_id,
            before=before,
            limit=limit + 1,
            skill_name=skill_name,
            device_id=device_id,
        )
        page = found[:limit]
        more = len(found) > limit
        return AuditList(records=tuple(page), next_before=page[-1].occurred_at if more else None)
