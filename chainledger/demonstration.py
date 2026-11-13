from .service import SupplyChain
from .analysis.study import verify_study
import tempfile
from pathlib import Path


def run():
    with SupplyChain() as app:
        app.consumer.register("supplier")
        app.consumer.register("factory")
        transform = {
            "id": "split",
            "source": "factory",
            "offset": 0,
            "occurred_at": "2026-11-11T01:00:00Z",
            "operations": [
                {
                    "id": "split-op",
                    "kind": "transform",
                    "inputs": [{"lot": "raw", "quantity": "10"}],
                    "outputs": [
                        {"lot": "child", "quantity": "9", "location": "warehouse"}
                    ],
                    "waste": "1",
                    "process": "cutting",
                }
            ],
        }
        blocked = app.consumer.submit(transform)
        app.consumer.submit(
            {
                "id": "receipt",
                "source": "supplier",
                "offset": 0,
                "occurred_at": "2026-11-11T00:00:00Z",
                "operations": [
                    {
                        "id": "receive-op",
                        "kind": "receive",
                        "lot": "raw",
                        "quantity": "10",
                        "material": "alloy",
                        "unit": "kg",
                        "supplier": "synthetic-supplier",
                        "location": "inbox",
                    }
                ],
            }
        )
        app.consumer.submit(
            {
                "id": "shipment-event",
                "source": "factory",
                "offset": 1,
                "occurred_at": "2026-11-11T02:00:00Z",
                "operations": [
                    {
                        "id": "ship-op",
                        "kind": "ship",
                        "lot": "child",
                        "quantity": "4",
                        "location": "warehouse",
                        "customer": "customer-A",
                        "shipment": "shipment-A",
                    }
                ],
            }
        )
        recall = app.recalls.open("recall-A", "raw", "Synthetic trace exercise")
        conserved = app.trace.conservation_report()
        assert blocked["state"] == "blocked"
        assert app.consumer.status("split")["state"] == "applied"
        assert app.graph.stock("child") == 5
        assert recall["impact"]["shipments"][0]["path"] == ["raw", "child"]
        assert conserved["materials"][0]["received"] == "10"
        assert conserved["materials"][0]["waste"] == "1"
        return {
            "initial_state": blocked["state"],
            "resolved_state": app.consumer.status("split")["state"],
            "inventory": app.inventory.by_location(),
            "recall": recall,
            "conservation": conserved,
            "check": app.check(),
        }


def study_walkthrough():
    with SupplyChain() as app:
        app.consumer.register("suppliers")
        app.quality.specification(
            {
                "id": "spec-alloy",
                "material": "alloy",
                "minimum_samples": 2,
                "maximum_defects": 1,
                "criteria": [
                    {"name": "hardness", "unit": "HB", "lower": "10", "upper": "20"}
                ],
            }
        )
        offset = 0

        def receive(lot, supplier, at):
            nonlocal offset
            result = app.consumer.submit(
                {
                    "id": "receipt-" + lot,
                    "source": "suppliers",
                    "offset": offset,
                    "occurred_at": at,
                    "operations": [
                        {
                            "id": "receive-" + lot,
                            "kind": "receive",
                            "lot": lot,
                            "quantity": "500",
                            "material": "alloy",
                            "unit": "kg",
                            "supplier": supplier,
                            "location": "inbox",
                        }
                    ],
                }
            )
            offset += 1
            assert result["state"] == "applied"

        def inspect(lot, inspection, defects, total, sampling="random"):
            app.quality.open(inspection, lot, "spec-alloy")
            revision = 1
            for index in range(total):
                value = "25" if index < defects else "15"
                app.quality.sample(
                    inspection,
                    inspection + "-" + str(index + 1),
                    {"hardness": {"value": value, "unit": "HB"}},
                    revision,
                    sampling=sampling,
                )
                revision += 1
            assessment = app.quality.evaluate(inspection)
            app.quality.disposition(
                "disposition-" + inspection,
                inspection,
                revision,
                "qa-reviewer",
                assessment["passed"],
                "routine disposition under original release rules",
            )
            return assessment

        schedule = [
            ("bulk-1", "bulkco", "2026-09-07T00:00:00Z", 1),
            ("bulk-2", "bulkco", "2026-09-14T00:00:00Z", 1),
            ("bulk-3", "bulkco", "2026-09-21T00:00:00Z", 0),
            ("bulk-4", "bulkco", "2026-09-28T00:00:00Z", 1),
            ("newco-a-1", "newco-a", "2026-09-29T00:00:00Z", 0),
            ("bulk-5", "bulkco", "2026-10-05T00:00:00Z", 2),
            ("newco-b-1", "newco-b", "2026-10-06T00:00:00Z", 2),
            ("bulk-6", "bulkco", "2026-10-12T00:00:00Z", 1),
            ("bulk-7", "bulkco", "2026-10-19T00:00:00Z", 0),
            ("bulk-8", "bulkco", "2026-10-26T00:00:00Z", 1),
        ]
        assessments = {}
        for lot, supplier, at, defects in schedule:
            receive(lot, supplier, at)
            total = 2 if supplier.startswith("newco") else 20
            assessments[lot] = inspect(lot, "inspection-" + lot, defects, total)
        receive("bulk-9", "bulkco", "2026-10-27T00:00:00Z")
        inspect("bulk-9", "inspection-bulk-9", 1, 5, sampling="convenience")
        assert not assessments["bulk-5"]["passed"]
        assert assessments["bulk-1"]["passed"]

        holds_before = len(app.journal.rows("SELECT id FROM holds"))
        config = {
            "id": "study-2026-10-alloy",
            "spec": "spec-alloy",
            "from": "2026-09-01T00:00:00Z",
            "to": "2026-11-01T00:00:00Z",
            "as_of": "2026-10-24T00:00:00Z",
            "unacceptable_rate": 0.08,
        }
        first = app.studies.create(config)
        holds_after = len(app.journal.rows("SELECT id FROM holds"))
        assert holds_before == holds_after
        study = app.studies.get(first["id"])["payload"]
        suppliers = {item["supplier"]: item for item in study["suppliers"]}
        newco_a = suppliers["newco-a"]
        newco_b = suppliers["newco-b"]
        bulkco = suppliers["bulkco"]
        assert newco_a["shrunk"]["mean"] > newco_a["raw_rate"]
        assert newco_a["shrunk"]["mean"] < newco_a["independent"]["mean"]
        assert newco_b["shrunk"]["mean"] < newco_b["raw_rate"]
        assert newco_b["shrunk"]["mean"] > bulkco["shrunk"]["mean"]
        assert bulkco["interval_width"] < newco_a["interval_width"]
        assert bulkco["interval_width"] < newco_b["interval_width"]
        uncertainty = {
            entry["supplier"]
            for entry in study["recommendations"]["high_uncertainty"]
        }
        assert {"newco-a", "newco-b"} <= uncertainty
        assert study["ranking"]["naive_by_total_defects"][-1] == "bulkco"
        assert study["ranking"]["naive_by_raw_rate"][0] == "newco-a"
        assert study["ranking"]["by_exceedance"][0] == "newco-b"
        assert study["evidence"]["convenience"]["inferable"] is False
        assert len(study["evidence"]["convenience"]["batches"]) == 1
        assert study["validation"]["status"] == "ok"

        app.quality.recheck(
            "inspection-newco-b-1",
            "inspection-newco-b-1-2",
            "recheck-1",
            {"hardness": {"value": "15", "unit": "HB"}},
            "2026-10-25T00:00:00Z",
            supersedes=True,
        )
        second = app.studies.create(
            {
                **config,
                "id": "study-2026-10-alloy-rev2",
                "as_of": "2026-11-01T00:00:00Z",
            }
        )
        comparison = app.studies.compare(first["id"], second["id"])
        newco_b_change = next(
            item
            for item in comparison["ranking_changes"]
            if item["supplier"] == "newco-b"
        )
        assert newco_b_change["exceedance_second"] < newco_b_change["exceedance_first"]
        assert comparison["evidence_changes"]["adjustments_added"]
        assert any(
            item["lot"] == "newco-b-1" and item["defects_second"] == 1
            for item in comparison["evidence_changes"]["batches_changed"]
        )

        with tempfile.TemporaryDirectory() as directory:
            first_export = app.studies.export(
                first["id"], str(Path(directory) / "study-first.json")
            )
            second_export = app.studies.export(
                second["id"], str(Path(directory) / "study-second.json")
            )
            verified = verify_study(first_export["path"])
            assert verified["id"] == first["id"]
            exports = {
                "first": first_export["path"],
                "second": second_export["path"],
                "verified": verified,
            }
        reused = app.studies.create(config)
        assert reused["reused"]
        return {
            "study": first["id"],
            "fit": study["fit"]["status"],
            "prior": study["fit"]["prior"],
            "ranking": study["ranking"],
            "recommendations": study["recommendations"],
            "suppliers": [
                {
                    "supplier": item["supplier"],
                    "samples": item["samples"],
                    "defects": item["defects"],
                    "raw_rate": item["raw_rate"],
                    "shrunk_mean": item["shrunk"]["mean"],
                    "interval": item["shrunk"]["interval"],
                    "exceedance": item["exceedance"],
                    "categories": item["categories"],
                }
                for item in study["suppliers"]
            ],
            "validation": {
                key: study["validation"][key]
                for key in ("status", "batches_scored", "coverage", "log_score")
            },
            "comparison": {
                "newco_b": newco_b_change,
                "adjustments_added": comparison["evidence_changes"][
                    "adjustments_added"
                ],
            },
            "holds_unchanged": holds_before == holds_after,
            "exports": exports,
            "check": app.check(),
        }
