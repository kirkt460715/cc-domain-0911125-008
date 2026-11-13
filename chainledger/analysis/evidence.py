from ..types import decode


def extract_evidence(journal, spec_id, start, end):
    rows = journal.rows(
        """SELECT i.id AS inspection,i.lot,l.supplier,l.created_at FROM inspections i
        JOIN lots l ON l.id=i.lot WHERE i.spec=? AND l.created_at>=? AND l.created_at<?
        ORDER BY l.created_at,l.id,i.id""",
        (spec_id, start, end),
    )
    batches = {}
    for row in rows:
        batch = batches.setdefault(
            row["lot"],
            {
                "lot": row["lot"],
                "supplier": row["supplier"],
                "received_at": row["created_at"],
                "inspections": [],
                "samples": [],
            },
        )
        batch["inspections"].append(row["inspection"])
        for sample in journal.rows(
            "SELECT id,payload FROM inspection_samples WHERE inspection=? ORDER BY id",
            (row["inspection"],),
        ):
            payload = decode(sample["payload"])
            batch["samples"].append(
                {
                    "inspection": row["inspection"],
                    "id": sample["id"],
                    "sampling": payload.get("sampling", "convenience"),
                    "defective": bool(payload["failed_criteria"]),
                    "rechecks": [],
                    "withdrawn": None,
                }
            )
    for batch in batches.values():
        for sample in batch["samples"]:
            for recheck in journal.rows(
                "SELECT id,recorded_at,payload FROM inspection_rechecks WHERE inspection=? AND sample=? ORDER BY recorded_at,id",
                (sample["inspection"], sample["id"]),
            ):
                payload = decode(recheck["payload"])
                sample["rechecks"].append(
                    {
                        "id": recheck["id"],
                        "recorded_at": recheck["recorded_at"],
                        "defective": bool(payload["failed_criteria"]),
                        "supersedes": bool(payload["supersedes"]),
                    }
                )
            withdrawn = journal.one(
                "SELECT recorded_at,reason FROM inspection_withdrawals WHERE inspection=? AND sample=?",
                (sample["inspection"], sample["id"]),
            )
            if withdrawn:
                sample["withdrawn"] = {
                    "recorded_at": withdrawn["recorded_at"],
                    "reason": withdrawn["reason"],
                }
    return [
        batches[lot]
        for lot in sorted(batches, key=lambda lot: (batches[lot]["received_at"], lot))
    ]


def _known(recorded_at, as_of):
    return as_of is None or recorded_at <= as_of


def batch_counts(batch, sampling, as_of):
    samples = 0
    defects = 0
    meta = {"withdrawn_samples": 0, "rechecks_applied": 0, "repeated_measurements": 0}
    for sample in batch["samples"]:
        if sample["sampling"] != sampling:
            continue
        if sample["withdrawn"] and _known(sample["withdrawn"]["recorded_at"], as_of):
            meta["withdrawn_samples"] += 1
            continue
        defective = sample["defective"]
        for recheck in sorted(
            sample["rechecks"], key=lambda item: (item["recorded_at"], item["id"])
        ):
            if not _known(recheck["recorded_at"], as_of):
                continue
            if recheck["supersedes"]:
                defective = recheck["defective"]
                meta["rechecks_applied"] += 1
            else:
                meta["repeated_measurements"] += 1
        samples += 1
        defects += 1 if defective else 0
    return {"samples": samples, "defects": defects, **meta}


def applied_adjustments(batches, as_of):
    adjustments = []
    for batch in batches:
        for sample in batch["samples"]:
            if sample["withdrawn"] and _known(sample["withdrawn"]["recorded_at"], as_of):
                adjustments.append(
                    {
                        "kind": "withdrawal",
                        "inspection": sample["inspection"],
                        "sample": sample["id"],
                        "recorded_at": sample["withdrawn"]["recorded_at"],
                    }
                )
            for recheck in sample["rechecks"]:
                if _known(recheck["recorded_at"], as_of):
                    adjustments.append(
                        {
                            "kind": "recheck",
                            "inspection": sample["inspection"],
                            "sample": sample["id"],
                            "id": recheck["id"],
                            "recorded_at": recheck["recorded_at"],
                            "supersedes": recheck["supersedes"],
                        }
                    )
    adjustments.sort(
        key=lambda item: (
            item["recorded_at"],
            item["inspection"],
            item["sample"],
            item.get("id", ""),
        )
    )
    return adjustments
