"""
economy.py — the credit system.

_move_money and _get_balance moved here verbatim from profiles_server.py.
Same signatures, same behaviour. Only the REASON_WORLD_* constants are new.
"""

from typing import Optional

from fastapi import HTTPException

# ── config ───────────────────────────────────────────────────────────────────
STARTING_GRANT = 2500
MIN_TRANSFER = 1
HISTORY_PAGE_SIZE = 50
HISTORY_MAX_PAGE_SIZE = 100

# reason values in use. Extend freely — this is a TEXT column, not a pg enum,
# so adding one is a code change rather than a migration.
REASON_STARTING_GRANT = "starting_grant"
REASON_TRANSFER = "transfer"
REASON_CHARACTER_DELETED = "character_deleted"

# world marketplace. reference_id carries the world_id (both uuid, no cast).
REASON_WORLD_PURCHASE = "world_purchase"     # buyer -> seller, full price
REASON_WORLD_ROYALTY = "world_royalty"       # seller -> original creator
REASON_WORLD_FEE = "world_marketplace_fee"   # seller -> burned

# The marketplace pot is not a table. It is:
#   SELECT COALESCE(SUM(amount), 0) FROM transactions
#   WHERE reason = 'world_marketplace_fee';
# minus whatever gets paid back out later. The ledger is the source of truth.


# ── the one money primitive ──────────────────────────────────────────────────
def _move_money(
        cur,
        from_character_id: Optional[str],
        to_character_id: Optional[str],
        amount: int,
        reason: str,
        reference_id: Optional[str] = None,
):
    """
    The single place money moves. Every path calls this — transfers, grants,
    purchases, presence pay later.

    from_character_id None -> minted into existence (grant, salary)
    to_character_id   None -> burned out of existence (purchase, delete)

    Must be called inside an existing cursor/transaction so the balance updates
    and the ledger row commit together or not at all.

    Raises HTTPException(409) if the sender cannot cover it.
    """
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")

    if from_character_id:
        # Conditional UPDATE rather than SELECT-then-UPDATE: the balance check
        # and the debit are one atomic statement, so two concurrent transfers
        # cannot both pass the check.
        cur.execute(
            """
            UPDATE characters
            SET balance = balance - %s
            WHERE id = %s AND balance >= %s
            RETURNING balance;
            """,
            (amount, from_character_id, amount),
        )
        if cur.fetchone() is None:
            raise HTTPException(status_code=409, detail="Insufficient funds")

    if to_character_id:
        cur.execute(
            "UPDATE characters SET balance = balance + %s WHERE id = %s RETURNING balance;",
            (amount, to_character_id),
        )
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Recipient character not found")

    cur.execute(
        """
        INSERT INTO transactions
            (from_character_id, to_character_id, amount, reason, reference_id)
        VALUES (%s, %s, %s, %s, %s);
        """,
        (from_character_id, to_character_id, amount, reason, reference_id),
    )


def _get_balance(cur, character_id: str) -> int:
    cur.execute("SELECT balance FROM characters WHERE id = %s;", (character_id,))
    row = cur.fetchone()
    return int(row[0]) if row else 0