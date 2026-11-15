"""Seed the Relate store with reference data and fictional sample onboardings.

Idempotent: constraints use IF NOT EXISTS, reference data uses MERGE and
onboarding ids are deterministic, so running it twice changes nothing.

Relate allows one writer per store. Stop the API first (``task stop`` or stop
``task dev``); the API also seeds reference data itself on every start.

Usage: uv run task graph-seed [-- --no-samples] [-- --store PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from pydantic import TypeAdapter

from email_service.agents.base import ALL_AGENTS
from email_service.config import Settings
from email_service.dependencies import utc_now
from email_service.exceptions import GraphUnavailableError
from email_service.graph.database import RelateDatabase
from email_service.graph.repositories import GraphRepository
from email_service.models.onboarding import OnboardingRequest
from email_service.onboarding.templates import DEFAULT_TEMPLATES
from email_service.services.onboarding_service import build_onboarding

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples" / "onboardings.json"


async def seed(store: str, samples: bool) -> int:
    settings = Settings()
    db = RelateDatabase(store)
    try:
        await db.open()
    except GraphUnavailableError as exc:
        print(f"Cannot open {store}: {exc.message}", file=sys.stderr)
        print("Is the API running? Relate allows a single writer per store.", file=sys.stderr)
        return 1
    try:
        repo = GraphRepository(db, utc_now)
        await repo.ensure_schema()
        providers = [(settings.email_primary_provider, "primary", True)]
        if settings.email_secondary_provider:
            providers.append(
                (settings.email_secondary_provider, "secondary", settings.email_failover_enabled)
            )
        await repo.seed_reference_data(
            DEFAULT_TEMPLATES,
            [(a.id, a.name, a.kind, a.description) for a in ALL_AGENTS],
            providers,
        )
        print(f"Reference data: {len(DEFAULT_TEMPLATES)} templates, {len(ALL_AGENTS)} agents.")
        if samples:
            adapter: TypeAdapter[object] = TypeAdapter(OnboardingRequest)
            records = json.loads(SAMPLES.read_text())["onboardings"]
            created = 0
            for record in records:
                request = adapter.validate_python(record)
                _, is_new = await repo.create_onboarding(
                    build_onboarding(request, "seed-script", utc_now())  # type: ignore[arg-type]
                )
                created += is_new
            print(
                f"Sample onboardings: {created} created, {len(records) - created} already present."
            )
        for label, count in (await repo.label_counts()).items():
            print(f"  {label:<14} {count}")
    finally:
        await db.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--store", default=None, help="store path (default: RELATE_STORE_PATH)")
    parser.add_argument("--no-samples", action="store_true", help="reference data only")
    args = parser.parse_args()
    store = args.store or Settings().relate_store_path
    return asyncio.run(seed(store, samples=not args.no_samples))


if __name__ == "__main__":
    raise SystemExit(main())
