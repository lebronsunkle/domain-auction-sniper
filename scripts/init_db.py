"""
Initialize the database with the default singleton system_settings row.

Run this once after migrations to make sure the safety governors module has
a settings row to read. It's idempotent — running it twice is safe; it
won't duplicate or overwrite an existing row.

Default values (v1, conservative):
  per_transaction_cap_dollars:  $25.00
  daily_spend_cap_dollars:      $50.00
  closeout_only_mode:           True   (v1 scope is closeouts-only)
  kill_switch_active:           False
  sanity_check_multiplier:      10.0   (target >10x floor requires confirmation)

Usage:
    alembic upgrade head           # first, run migrations
    python scripts/init_db.py      # then, seed settings
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models.settings import SystemSettings  # noqa: E402


async def main() -> int:
    print("Initializing database...")

    async with SessionLocal() as session:
        # Check whether the singleton row already exists.
        result = await session.execute(
            select(SystemSettings).where(SystemSettings.id == 1)
        )
        existing = result.scalar_one_or_none()

        if existing is not None:
            print("system_settings row already exists. Leaving values as-is:")
            print(f"  per_transaction_cap_dollars: ${existing.per_transaction_cap_dollars}")
            print(f"  daily_spend_cap_dollars:     ${existing.daily_spend_cap_dollars}")
            print(f"  closeout_only_mode:          {existing.closeout_only_mode}")
            print(f"  kill_switch_active:          {existing.kill_switch_active}")
            print(f"  sanity_check_multiplier:     {existing.sanity_check_multiplier}")
            print("\nTo change any of these, update them via the API/dashboard "
                  "(coming in Phase 1) or by hand in psql/TablePlus.")
            return 0

        # Create the row.
        settings = SystemSettings(
            id=1,
            per_transaction_cap_dollars=Decimal("25.00"),
            daily_spend_cap_dollars=Decimal("50.00"),
            closeout_only_mode=True,    # v1 scope
            kill_switch_active=False,
            kill_switch_reason="",
            sanity_check_multiplier=Decimal("10.00"),
            updated_at=datetime.now(timezone.utc),
        )
        session.add(settings)
        await session.commit()

        print("Created system_settings row with v1 defaults:")
        print(f"  per_transaction_cap_dollars: $25.00")
        print(f"  daily_spend_cap_dollars:     $50.00")
        print(f"  closeout_only_mode:          True   (bidding deferred in v1)")
        print(f"  kill_switch_active:          False")
        print(f"  sanity_check_multiplier:     10.00")
        print()
        print("The safety governors module will now refuse any action that "
              "violates these limits.")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
