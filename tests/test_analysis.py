import json
import tempfile
import unittest
from pathlib import Path
from helpers import app as new_app, envelope, receive, T0, T1, T2
from chainledger.types import ChainError, timestamp
from chainledger.analysis import model
from chainledger.analysis.study import verify_study

T3 = "2026-11-14T00:00:00Z"
T4 = "2026-11-15T00:00:00Z"
T5 = "2026-11-16T00:00:00Z"
T6 = "2026-11-17T00:00:00Z"

SPEC = {
    "id": "spec",
    "material": "alloy",
    "minimum_samples": 2,
    "maximum_defects": 1,
    "criteria": [{"name": "hardness", "unit": "HB", "lower": "10", "upper": "20"}],
}


def quality_app():
    application = new_app()
    application.quality.specification(SPEC)
    return application


def receive_lot(application, lot, supplier, at, offset, quantity="100"):
    result = application.consumer.submit(
        envelope(
            "receipt-" + lot,
            "s",
            offset,
            [receive(lot=lot, ident="receive-" + lot, supplier=supplier, quantity=quantity)],
            at,
        )
    )
    assert result["state"] == "applied"
    return result


def add_samples(application, inspection, values, sampling="random"):
    for index, value in enumerate(values):
        application.quality.sample(
            inspection,
            inspection + "-s" + str(index),
            {"hardness": {"value": value, "unit": "HB"}},
            index + 1,
            sampling=sampling,
        )


def study_payload(application, study_id="study", **overrides):
    config = {"id": study_id, "spec": "spec", "unacceptable_rate": 0.1}
    config.update(overrides)
    created = application.studies.create(config)
    return application.studies.get(created["id"])["payload"]


def supplier(payload, name):
    return next(item for item in payload["suppliers"] if item["supplier"] == name)


class CountingRuleTests(unittest.TestCase):
    def setUp(self):
        self.app = quality_app()
        self.addCleanup(self.app.close)
        receive_lot(self.app, "l1", "sup-a", T0, 0, quantity="10000")
        self.app.quality.open("i1", "l1", "spec")
        add_samples(self.app, "i1", ["25", "15", "15"])

    def test_independent_sample_counting_rules(self):
        self.app.quality.recheck(
            "i1",
            "i1-s0",
            "rc1",
            {"hardness": {"value": "15", "unit": "HB"}},
            T2,
            supersedes=True,
        )
        self.app.quality.recheck(
            "i1",
            "i1-s1",
            "rc2",
            {"hardness": {"value": "15", "unit": "HB"}},
            T2,
            supersedes=False,
        )
        self.app.quality.withdraw_sample("i1", "i1-s2", T2, "sample contaminated")
        payload = study_payload(self.app)
        batch = payload["evidence"]["random"]["batches"][0]
        self.assertEqual(batch["samples"], 2)
        self.assertEqual(batch["defects"], 0)
        counting = payload["evidence"]["counting"]
        self.assertEqual(counting["withdrawn_samples"], 1)
        self.assertEqual(counting["rechecks_applied"], 1)
        self.assertEqual(counting["repeated_measurements"], 1)
        self.assertEqual(counting["unit"], "independent_physical_sample")

    def test_later_rechecks_and_withdrawals_respect_as_of(self):
        self.app.quality.recheck(
            "i1",
            "i1-s0",
            "rc1",
            {"hardness": {"value": "15", "unit": "HB"}},
            T2,
            supersedes=True,
        )
        self.app.quality.withdraw_sample("i1", "i1-s2", T2, "sample contaminated")
        payload = study_payload(self.app, as_of="2026-11-11T01:30:00Z")
        batch = payload["evidence"]["random"]["batches"][0]
        self.assertEqual(batch["samples"], 3)
        self.assertEqual(batch["defects"], 1)
        self.assertEqual(payload["evidence"]["adjustments"], [])

    def test_weighed_quantity_never_adds_samples(self):
        payload = study_payload(self.app)
        batch = payload["evidence"]["random"]["batches"][0]
        self.assertEqual(batch["samples"], 3)
        self.assertEqual(
            self.app.graph.lot("l1")["quantity"], "10000"
        )

    def test_recheck_and_withdrawal_identity_and_errors(self):
        self.app.quality.recheck(
            "i1",
            "i1-s0",
            "rc1",
            {"hardness": {"value": "15", "unit": "HB"}},
            T2,
            supersedes=True,
        )
        again = self.app.quality.recheck(
            "i1",
            "i1-s0",
            "rc1",
            {"hardness": {"value": "15", "unit": "HB"}},
            T2,
            supersedes=True,
        )
        self.assertEqual(again["id"], "rc1")
        with self.assertRaises(ChainError):
            self.app.quality.recheck(
                "i1",
                "i1-s0",
                "rc1",
                {"hardness": {"value": "25", "unit": "HB"}},
                T2,
                supersedes=True,
            )
        with self.assertRaises(ChainError):
            self.app.quality.recheck(
                "i1",
                "missing",
                "rc9",
                {"hardness": {"value": "15", "unit": "HB"}},
                T2,
            )
        self.app.quality.withdraw_sample("i1", "i1-s2", T2, "contaminated")
        with self.assertRaises(ChainError):
            self.app.quality.withdraw_sample("i1", "i1-s2", T2, "different reason")
        with self.assertRaises(ChainError):
            self.app.quality.sample(
                "i1", "sx", {"hardness": {"value": "15", "unit": "HB"}}, 4, sampling="stratified"
            )

    def test_release_evaluation_ignores_rechecks_and_withdrawals(self):
        before = self.app.quality.evaluate("i1")
        self.app.quality.recheck(
            "i1",
            "i1-s0",
            "rc1",
            {"hardness": {"value": "15", "unit": "HB"}},
            T2,
            supersedes=True,
        )
        self.app.quality.withdraw_sample("i1", "i1-s2", T2, "contaminated")
        after = self.app.quality.evaluate("i1")
        self.assertEqual(before, after)
        self.assertEqual(after["observed"], 3)
        self.assertEqual(after["defects"], ["i1-s0"])
        released = self.app.quality.disposition("rel", "i1", 4, "qa", True, "passes")
        self.assertTrue(released["release"])
        receive_lot(self.app, "l2", "sup-a", T1, 1)
        self.app.quality.open("i2", "l2", "spec")
        add_samples(self.app, "i2", ["25", "25"])
        self.assertFalse(self.app.quality.evaluate("i2")["passed"])
        with self.assertRaises(ChainError):
            self.app.quality.disposition("rel2", "i2", 3, "qa", True, "still failing")


class SamplingClassTests(unittest.TestCase):
    def setUp(self):
        self.app = quality_app()
        self.addCleanup(self.app.close)
        receive_lot(self.app, "l1", "sup-a", T0, 0)
        self.app.quality.open("i1", "l1", "spec")
        add_samples(self.app, "i1", ["25", "15", "15"], sampling="convenience")

    def test_convenience_samples_are_listed_but_never_inferred(self):
        receive_lot(self.app, "l2", "sup-b", T1, 1)
        self.app.quality.open("i2", "l2", "spec")
        add_samples(self.app, "i2", ["15", "15", "25"], sampling="random")
        payload = study_payload(self.app)
        convenience = payload["evidence"]["convenience"]
        self.assertFalse(convenience["inferable"])
        self.assertEqual(len(convenience["batches"]), 1)
        self.assertEqual(convenience["batches"][0]["samples"], 3)
        self.assertEqual(len(payload["evidence"]["random"]["batches"]), 1)
        self.assertEqual(payload["fit"]["status"], "fallback")

    def test_convenience_only_stratum_is_not_inferable(self):
        payload = study_payload(self.app)
        self.assertEqual(payload["fit"]["status"], "not_inferable")
        self.assertEqual(payload["suppliers"], [])
        self.assertEqual(payload["recommendations"]["high_risk"], [])
        self.assertEqual(payload["validation"]["status"], "skipped")
        self.assertEqual(len(payload["evidence"]["convenience"]["batches"]), 1)

    def test_samples_without_declared_method_are_convenience(self):
        receive_lot(self.app, "l3", "sup-c", T1, 1)
        self.app.quality.open("i3", "l3", "spec")
        self.app.quality.sample(
            "i3", "i3-s0", {"hardness": {"value": "15", "unit": "HB"}}, 1
        )
        payload = study_payload(self.app)
        self.assertEqual(payload["fit"]["status"], "not_inferable")
        self.assertEqual(len(payload["evidence"]["convenience"]["batches"]), 2)


class ModelFallbackTests(unittest.TestCase):
    def test_single_supplier_falls_back_to_uniform_prior(self):
        fit = model.fit_hyperparameters([{"supplier": "only", "samples": 5, "defects": 1}])
        self.assertEqual(fit["status"], "fallback")
        self.assertEqual(fit["prior"]["source"], "fallback_uniform")
        self.assertTrue(fit["notes"])

    def test_zero_defects_reports_boundary(self):
        batches = [
            {"supplier": "a", "samples": 10, "defects": 0},
            {"supplier": "a", "samples": 10, "defects": 0},
            {"supplier": "b", "samples": 10, "defects": 0},
            {"supplier": "b", "samples": 10, "defects": 0},
        ]
        fit = model.fit_hyperparameters(batches)
        self.assertIn("zero_defects", fit["boundaries"])
        self.assertIn(fit["status"], ("ml", "moments"))

    def test_all_identical_rates_report_boundary(self):
        batches = [
            {"supplier": "a", "samples": 20, "defects": 2},
            {"supplier": "a", "samples": 20, "defects": 2},
            {"supplier": "b", "samples": 20, "defects": 2},
            {"supplier": "b", "samples": 20, "defects": 2},
        ]
        fit = model.fit_hyperparameters(batches)
        self.assertTrue(fit["boundaries"])

    def test_tiny_sample_uses_moments_with_note(self):
        batches = [
            {"supplier": "a", "samples": 2, "defects": 0},
            {"supplier": "b", "samples": 2, "defects": 2},
        ]
        fit = model.fit_hyperparameters(batches)
        self.assertEqual(fit["status"], "moments")
        self.assertTrue(any("too few batches" in note for note in fit["notes"]))

    def test_external_prior_is_distinguished_from_empirical(self):
        batches = [
            {"supplier": "a", "samples": 10, "defects": 1},
            {"supplier": "b", "samples": 10, "defects": 2},
        ]
        fit = model.fit_hyperparameters(
            batches,
            mode="external",
            external={"a": 2.0, "b": 38.0, "reference": "prereg-2026-09"},
        )
        self.assertEqual(fit["status"], "external")
        self.assertEqual(fit["prior"]["source"], "external_preregistered")
        self.assertEqual(fit["prior"]["reference"], "prereg-2026-09")
        self.assertIn("empirical", fit)
        self.assertEqual(fit["prior"]["a"], 2.0)
        self.assertNotEqual(fit["empirical"]["a"], fit["prior"]["a"])

    def test_shrinkage_pulls_extreme_small_samples_toward_prior(self):
        batches = [{"supplier": "a", "samples": 2, "defects": 0}]
        batches += [{"supplier": "b", "samples": 2, "defects": 2}]
        batches += [
            {"supplier": "c", "samples": 25, "defects": 1} for _ in range(4)
        ]
        fit = model.fit_hyperparameters(batches)
        grouped = model.group_by_supplier(batches)
        post_a = model.supplier_posterior(grouped["a"], fit["prior"], 0.9, 0.1)
        post_b = model.supplier_posterior(grouped["b"], fit["prior"], 0.9, 0.1)
        post_c = model.supplier_posterior(grouped["c"], fit["prior"], 0.9, 0.1)
        self.assertGreater(post_a["mean"], 0.0)
        self.assertLess(post_b["mean"], 1.0)
        self.assertGreater(post_b["mean"], post_c["mean"])
        width_c = post_c["interval"][1] - post_c["interval"][0]
        width_a = post_a["interval"][1] - post_a["interval"][0]
        self.assertLess(width_c, width_a)


class StudyFlowTests(unittest.TestCase):
    def setUp(self):
        self.app = quality_app()
        self.addCleanup(self.app.close)
        lots = [
            ("a-1", "newco-a", T0, ["15", "15"]),
            ("b-1", "newco-b", T1, ["25", "25"]),
            ("c-1", "bulkco", T2, ["25", "15", "15", "15", "15", "15", "15", "15", "15", "15"]),
            ("c-2", "bulkco", T3, ["25", "15", "15", "15", "15", "15", "15", "15", "15", "15"]),
            ("c-3", "bulkco", T4, ["25", "15", "15", "15", "15", "15", "15", "15", "15", "15"]),
            ("c-4", "bulkco", T5, ["15"] * 10),
        ]
        for offset, (lot, supplier_name, at, values) in enumerate(lots):
            receive_lot(self.app, lot, supplier_name, at, offset)
            self.app.quality.open("inspection-" + lot, lot, "spec")
            add_samples(self.app, "inspection-" + lot, values)

    def test_shrinkage_recommendations_and_rankings(self):
        payload = study_payload(self.app)
        self.assertIn(payload["fit"]["status"], ("ml", "moments"))
        self.assertEqual(payload["fit"]["prior"]["source"], "empirical_bayes")
        newco_a = supplier(payload, "newco-a")
        newco_b = supplier(payload, "newco-b")
        bulkco = supplier(payload, "bulkco")
        self.assertGreater(newco_a["shrunk"]["mean"], newco_a["raw_rate"])
        self.assertLess(newco_a["shrunk"]["mean"], newco_a["independent"]["mean"])
        self.assertLess(newco_b["shrunk"]["mean"], newco_b["raw_rate"])
        self.assertGreater(newco_b["shrunk"]["mean"], bulkco["shrunk"]["mean"])
        self.assertLess(bulkco["interval_width"], newco_a["interval_width"])
        self.assertLess(bulkco["interval_width"], newco_b["interval_width"])
        uncertainty = {
            entry["supplier"] for entry in payload["recommendations"]["high_uncertainty"]
        }
        self.assertIn("newco-a", uncertainty)
        self.assertIn("newco-b", uncertainty)
        self.assertNotIn("bulkco", uncertainty)
        self.assertEqual(payload["ranking"]["naive_by_total_defects"][-1], "bulkco")
        self.assertEqual(payload["ranking"]["naive_by_raw_rate"][0], "newco-a")
        self.assertEqual(payload["ranking"]["by_exceedance"][0], "newco-b")
        self.assertTrue(
            any("never freezes material" in note for note in payload["notes"])
        )

    def test_study_never_freezes_material(self):
        before = len(self.app.journal.rows("SELECT id FROM holds"))
        study_payload(self.app)
        after = len(self.app.journal.rows("SELECT id FROM holds"))
        self.assertEqual(before, after)

    def test_study_is_frozen_idempotent_and_conflicts_on_change(self):
        config = {"id": "study", "spec": "spec", "unacceptable_rate": 0.1}
        first = self.app.studies.create(config)
        self.assertFalse(first["reused"])
        second = self.app.studies.create(config)
        self.assertTrue(second["reused"])
        self.assertEqual(first["digest"], second["digest"])
        with self.assertRaises(ChainError):
            self.app.studies.create({**config, "unacceptable_rate": 0.2})
        listed = self.app.studies.list()
        self.assertEqual([item["id"] for item in listed], ["study"])
        fetched = self.app.studies.get("study")
        self.assertEqual(fetched["digest"], first["digest"])

    def test_export_and_verification(self):
        study_payload(self.app)
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "study.json")
            exported = self.app.studies.export("study", path)
            verified = verify_study(path)
            self.assertEqual(verified["id"], "study")
            self.assertEqual(verified["digest"], exported["digest"])
            with self.assertRaises(ChainError):
                self.app.studies.export("study", path)
            data = json.loads(Path(path).read_text())
            data["payload"]["suppliers"] = []
            Path(path).write_text(json.dumps(data))
            with self.assertRaises(ChainError):
                verify_study(path)

    def test_rolling_validation_uses_only_past_evidence(self):
        self.app.quality.recheck(
            "inspection-c-3",
            "inspection-c-3-s0",
            "rc1",
            {"hardness": {"value": "15", "unit": "HB"}},
            "2026-11-15T12:00:00Z",
            supersedes=True,
        )
        payload = study_payload(self.app, as_of=T6)
        validation = payload["validation"]
        self.assertEqual(validation["status"], "ok")
        self.assertEqual(validation["scheme"], "rolling_origin_whole_batch_holdout")
        folds = validation["folds"]
        self.assertTrue(folds)
        for fold in folds:
            self.assertIn("training", fold)
            self.assertIn("empirical_bayes", fold)
            self.assertIn("independent", fold)
        cutoff_before_recheck = [
            fold for fold in folds if fold["cutoff"] < "2026-11-15T12:00:00Z"
        ]
        self.assertTrue(cutoff_before_recheck)
        batch_c3 = next(
            item
            for item in payload["evidence"]["random"]["batches"]
            if item["lot"] == "c-3"
        )
        self.assertEqual(batch_c3["defects"], 0)
        trained_with_c3 = [fold for fold in folds if fold["cutoff"] == timestamp(T4)]
        self.assertEqual(len(trained_with_c3), 1)
        self.assertEqual(trained_with_c3[0]["training"]["batches"], 5)
        self.assertEqual(trained_with_c3[0]["training"]["defects"], 5)
        self.assertIn("empirical_bayes", validation["coverage"])
        self.assertIn("independent", validation["coverage"])

    def test_compare_explains_ranking_change_from_corrections(self):
        first = study_payload(self.app, "first", as_of=T3)
        self.app.quality.recheck(
            "inspection-b-1",
            "inspection-b-1-s1",
            "rc1",
            {"hardness": {"value": "15", "unit": "HB"}},
            T4,
            supersedes=True,
        )
        second = study_payload(self.app, "second", as_of=T6)
        comparison = self.app.studies.compare("first", "second")
        change = next(
            item
            for item in comparison["ranking_changes"]
            if item["supplier"] == "newco-b"
        )
        self.assertLess(change["exceedance_second"], change["exceedance_first"])
        changed = comparison["evidence_changes"]["batches_changed"]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["lot"], "b-1")
        self.assertEqual(changed[0]["defects_first"], 2)
        self.assertEqual(changed[0]["defects_second"], 1)
        added = comparison["evidence_changes"]["adjustments_added"]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["kind"], "recheck")
        self.assertEqual(first["evidence"]["digest"], self.app.studies.get("first")["payload"]["evidence"]["digest"])
        self.assertNotEqual(
            first["evidence"]["digest"], second["evidence"]["digest"]
        )

    def test_external_prior_flow(self):
        payload = study_payload(
            self.app,
            prior={"mode": "external", "a": 2, "b": 38, "reference": "prereg-1"},
        )
        self.assertEqual(payload["fit"]["status"], "external")
        self.assertEqual(payload["fit"]["prior"]["source"], "external_preregistered")
        self.assertIn("empirical", payload["fit"])
        self.assertEqual(payload["fit"]["prior"]["reference"], "prereg-1")

    def test_missing_spec_or_empty_stratum_fails(self):
        with self.assertRaises(ChainError):
            self.app.studies.create(
                {"id": "bad", "spec": "missing", "unacceptable_rate": 0.1}
            )
        self.app.quality.specification({**SPEC, "id": "spec-empty"})
        with self.assertRaises(ChainError):
            self.app.studies.create(
                {"id": "empty", "spec": "spec-empty", "unacceptable_rate": 0.1}
            )
        with self.assertRaises(ChainError):
            self.app.studies.create(
                {"id": "bad-rate", "spec": "spec", "unacceptable_rate": 1.5}
            )
        with self.assertRaises(ChainError):
            self.app.studies.get("missing")
        with self.assertRaises(ChainError):
            self.app.studies.compare("first", "missing")

    def test_compare_requires_shared_spec_revision(self):
        self.app.quality.specification({**SPEC, "id": "spec-other"})
        receive_lot(self.app, "d-1", "newco-a", T0, 6)
        self.app.quality.open("inspection-d-1", "d-1", "spec-other")
        add_samples(self.app, "inspection-d-1", ["15", "15"])
        study_payload(self.app, "first")
        study_payload(self.app, "other", spec="spec-other")
        with self.assertRaises(ChainError):
            self.app.studies.compare("first", "other")


if __name__ == "__main__":
    unittest.main()
