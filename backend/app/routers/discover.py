"""Scan the machine for running applications and import them in bulk."""

import json
import sqlite3
from pathlib import Path

from fastapi import APIRouter

from ..database import db, record_event, utcnow
from ..discovery import discover
from ..models import Candidate, ImportRequest, ImportResult, ImportSkip
from ..service import fetch_row, to_out

router = APIRouter(prefix="/api/discover", tags=["discover"])


def _registered() -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT name, command, working_dir, port FROM apps").fetchall()
    return [dict(row) for row in rows]


@router.get("", response_model=list[Candidate])
def scan() -> list[Candidate]:
    return [Candidate(**item) for item in discover(_registered())]


@router.post("/import", response_model=ImportResult)
def import_apps(payload: ImportRequest) -> ImportResult:
    created_ids: list[int] = []
    skipped: list[ImportSkip] = []

    with db() as conn:
        taken = {row["name"] for row in conn.execute("SELECT name FROM apps").fetchall()}

        for item in payload.items:
            working_dir = str(Path(item.working_dir).expanduser())
            if not Path(working_dir).is_dir():
                skipped.append(ImportSkip(name=item.name, reason="working directory does not exist"))
                continue

            # Bulk import should not fail wholesale on a name clash, so make it unique.
            name = item.name
            suffix = 2
            while name in taken:
                name = f"{item.name}-{suffix}"
                suffix += 1

            now = utcnow()
            try:
                cursor = conn.execute(
                    """INSERT INTO apps (name, description, command, working_dir, port, env,
                                         auto_restart, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)""",
                    (name, item.description, item.command, working_dir, item.port, json.dumps({}), now, now),
                )
            except sqlite3.IntegrityError as exc:
                skipped.append(ImportSkip(name=item.name, reason=str(exc)))
                continue

            taken.add(name)
            created_ids.append(cursor.lastrowid)
            record_event(cursor.lastrowid, "created", f"imported from system scan (pid detected)")

    return ImportResult(
        created=[to_out(fetch_row(app_id)) for app_id in created_ids],
        skipped=skipped,
    )
