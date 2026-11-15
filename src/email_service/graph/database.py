"""Relate by Neo4j adapter.

Relate (https://relatedb.ai, Python package ``relate-db``, import ``relate``)
is an embedded, in-process Cypher graph database. Verified facts this adapter
is built on (relate-db 1.0.1 documentation, confirmed by local probing):

* ``relate.connect(path)`` opens a directory store, a single ``.rl`` file or
  ``:memory:``; every call is synchronous.
* A connection belongs to the thread that opened it (``threadsafety == 1``);
  other threads get ``ProgrammingError`` with kind ``WrongThread``.
* A file-backed store accepts **one writable connection at a time**, in this
  process or any other; a second open fails with ``storage locked``.
* ``conn.autocommit = False`` starts explicit transactions; ``commit()`` /
  ``rollback()`` end them. A write-write conflict raises an error whose
  ``retryable`` attribute is ``True``.

Therefore this adapter owns exactly one connection, pinned to one dedicated
worker thread, and every graph operation is submitted to that thread. That
serialises writes inside the process, which is also what makes the
"claim a draft for sending" step atomic. Never run two application replicas
against the same store path.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import relate

from email_service.exceptions import GraphUnavailableError

logger = logging.getLogger(__name__)

Row = dict[str, Any]
MAX_TRANSACTION_RETRIES = 3


class Tx:
    """Thin wrapper over a Relate connection used inside a unit of work."""

    def __init__(self, conn: relate.Connection) -> None:
        self._conn = conn

    def run(self, statement: str, params: dict[str, Any] | None = None) -> list[Row]:
        cursor = self._conn.execute(statement, params or {})
        return [dict(row.items()) for row in cursor.fetchall()]

    def single(self, statement: str, params: dict[str, Any] | None = None) -> Row | None:
        rows = self.run(statement, params)
        return rows[0] if rows else None


class RelateDatabase:
    def __init__(self, store_path: str, *, read_only: bool = False) -> None:
        self.store_path = store_path
        self.read_only = read_only
        self._executor: ThreadPoolExecutor | None = None
        self._conn: relate.Connection | None = None

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    async def open(self) -> None:
        if self._conn is not None:
            return
        if self.store_path != ":memory:" and not self.read_only:
            Path(self.store_path).parent.mkdir(parents=True, exist_ok=True)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="relate-graph")
        loop = asyncio.get_running_loop()
        try:
            self._conn = await loop.run_in_executor(
                self._executor,
                lambda: relate.connect(self.store_path, read_only=self.read_only),
            )
        except relate.Error as exc:
            self._executor.shutdown(wait=False)
            self._executor = None
            raise GraphUnavailableError(
                f"cannot open Relate store: {exc}", details={"kind": getattr(exc, "kind", None)}
            ) from exc
        logger.info("relate store opened", extra={"store": self.store_path})

    async def close(self) -> None:
        if self._conn is None or self._executor is None:
            return
        conn, executor = self._conn, self._executor
        self._conn = None
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(executor, conn.close)  # close on the owning thread
        executor.shutdown(wait=True)
        self._executor = None

    async def _submit[T](self, fn: Callable[[relate.Connection], T]) -> T:
        if self._conn is None or self._executor is None:
            raise GraphUnavailableError("graph database is not open")
        conn = self._conn
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, lambda: fn(conn))

    async def read[T](self, work: Callable[[Tx], T]) -> T:
        """Run read-only work in autocommit mode."""
        return await self._submit(lambda conn: work(Tx(conn)))

    async def write[T](self, work: Callable[[Tx], T]) -> T:
        """Run ``work`` in one explicit transaction, retrying retryable conflicts."""

        def unit(conn: relate.Connection) -> T:
            for attempt in range(1, MAX_TRANSACTION_RETRIES + 1):
                conn.autocommit = False
                try:
                    result = work(Tx(conn))
                    conn.commit()
                    return result
                except relate.Error as exc:
                    conn.rollback()
                    if getattr(exc, "retryable", False) and attempt < MAX_TRANSACTION_RETRIES:
                        logger.warning("retrying graph transaction after conflict")
                        continue
                    raise
                except BaseException:
                    conn.rollback()
                    raise
                finally:
                    conn.autocommit = True
            raise AssertionError("unreachable")  # pragma: no cover

        return await self._submit(unit)

    async def ping(self) -> bool:
        try:
            row = await self.read(lambda tx: tx.single("RETURN 1 AS ok"))
        except Exception:
            return False
        return bool(row and row["ok"] == 1)
