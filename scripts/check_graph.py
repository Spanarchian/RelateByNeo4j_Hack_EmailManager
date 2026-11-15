"""Read-only inspection of the Relate store.

Opens the store with ``read_only=True``, which Relate allows beside a running
writer for directory stores, so this works while the API is running.

Usage: uv run task graph-check [-- --store PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from email_service.config import Settings
from email_service.dependencies import utc_now
from email_service.exceptions import GraphUnavailableError
from email_service.graph.database import RelateDatabase
from email_service.graph.repositories import GraphRepository
from email_service.models.email import DraftStatus
from email_service.models.onboarding import OnboardingType


async def check(store: str) -> int:
    db = RelateDatabase(store, read_only=True)
    try:
        await db.open()
    except GraphUnavailableError as exc:
        print(f"Cannot open {store} read-only: {exc.message}", file=sys.stderr)
        return 1
    try:
        repo = GraphRepository(db, utc_now)
        print(f"Relate store: {store}")
        print("Nodes:")
        for label, count in (await repo.label_counts()).items():
            print(f"  {label:<14} {count}")
        constraints = await db.read(lambda tx: tx.run("SHOW CONSTRAINTS"))
        print(f"Constraints: {len(constraints)}")
        volunteers = await repo.list_onboardings(OnboardingType.VOLUNTEER)
        employees = await repo.list_onboardings(OnboardingType.EMPLOYEE)
        print(f"Onboardings: {len(volunteers)} volunteer, {len(employees)} employee")
        print(f"Pending approvals: {len(await repo.list_drafts([DraftStatus.PENDING_REVIEW]))}")
        print(f"Unsent onboarding emails: {len(await repo.list_unsent_onboarding_drafts())}")
        failed = await repo.list_failed_deliveries()
        print(f"Failed / unknown deliveries: {len(failed)}")
        for delivery in failed:
            print(f"  {delivery.status.value:<8} draft={delivery.draft_id} delivery={delivery.id}")
    finally:
        await db.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", default=None, help="store path (default: RELATE_STORE_PATH)")
    args = parser.parse_args()
    return asyncio.run(check(args.store or Settings().relate_store_path))


if __name__ == "__main__":
    raise SystemExit(main())
