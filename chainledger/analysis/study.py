import math
from ..types import key, obj, timestamp, canonical, decode, fingerprint, fail
from ..events.exchange import write_new, read_file
from .evidence import extract_evidence, batch_counts, applied_adjustments
from . import model

STUDY_KIND = "supplier_quality_study"
MIN_TRAIN_BATCHES = 3


def _rate(value, field, low=0.0, high=1.0):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not low < value < high
    ):
        fail("input", "invalid probability", field=field)
    return float(value)


def _positive(value, field):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0.0 < value <= 1e6
    ):
        fail("input", "invalid positive number", field=field)
    return float(value)


def _config(data):
    obj(
        data,
        {
            "id",
            "spec",
            "from",
            "to",
            "as_of",
            "unacceptable_rate",
            "interval_level",
            "risk_threshold",
            "uncertainty_width",
            "prior",
        },
        {"id", "spec", "unacceptable_rate"},
    )
    config = {
        "id": key(data["id"]),
        "spec": key(data["spec"]),
        "from": timestamp(data["from"]) if "from" in data else "1970-01-01T00:00:00Z",
        "to": timestamp(data["to"]) if "to" in data else "2200-01-01T00:00:00Z",
        "as_of": timestamp(data["as_of"]) if "as_of" in data else None,
        "unacceptable_rate": _rate(data["unacceptable_rate"], "unacceptable_rate"),
        "interval_level": _rate(
            data.get("interval_level", 0.9), "interval_level", 0.5, 0.999
        ),
        "risk_threshold": _rate(data.get("risk_threshold", 0.5), "risk_threshold"),
        "uncertainty_width": _rate(
            data.get("uncertainty_width", 0.2), "uncertainty_width"
        ),
    }
    if not config["from"] < config["to"]:
        fail("input", "study time range must increase")
    prior = data.get("prior", {"mode": "empirical"})
    obj(prior, {"mode", "a", "b", "kappa", "reference"}, {"mode"})
    mode = prior["mode"]
    if mode not in ("empirical", "external"):
        fail("input", "unknown prior mode", mode=mode)
    normalized = {"mode": mode}
    if mode == "external":
        missing = {"a", "b"} - prior.keys()
        if missing:
            fail("input", "external prior requires a and b", fields=sorted(missing))
        normalized["a"] = _positive(prior["a"], "a")
        normalized["b"] = _positive(prior["b"], "b")
        if "kappa" in prior:
            normalized["kappa"] = _positive(prior["kappa"], "kappa")
        if "reference" in prior:
            reference = prior["reference"]
            if (
                not isinstance(reference, str)
                or not reference.strip()
                or len(reference) > 200
            ):
                fail("input", "invalid prior reference")
            normalized["reference"] = reference.strip()
    config["prior"] = normalized
    return config


def _finalize(batches, as_of):
    random_batches = []
    convenience_batches = []
    counting = {
        "withdrawn_samples": 0,
        "rechecks_applied": 0,
        "repeated_measurements": 0,
    }
    for batch in batches:
        for sampling, target in (
            ("random", random_batches),
            ("convenience", convenience_batches),
        ):
            counts = batch_counts(batch, sampling, as_of)
            for field in counting:
                counting[field] += counts[field]
            if counts["samples"]:
                target.append(
                    {
                        "lot": batch["lot"],
                        "supplier": batch["supplier"],
                        "received_at": batch["received_at"],
                        "samples": counts["samples"],
                        "defects": counts["defects"],
                    }
                )
    return random_batches, convenience_batches, counting


def _recommendations(suppliers):
    def entry(supplier):
        return {
            "supplier": supplier["supplier"],
            "exceedance": supplier["exceedance"],
            "interval_width": supplier["interval_width"],
            "shrunk_mean": supplier["shrunk"]["mean"],
            "samples": supplier["samples"],
            "batches": supplier["batches"],
        }

    high_risk = sorted(
        (item for item in suppliers if "high_risk" in item["categories"]),
        key=lambda item: (-item["exceedance"], item["supplier"]),
    )
    high_uncertainty = sorted(
        (item for item in suppliers if "high_uncertainty" in item["categories"]),
        key=lambda item: (-item["interval_width"], item["supplier"]),
    )
    return {
        "high_risk": [entry(item) for item in high_risk],
        "high_uncertainty": [entry(item) for item in high_uncertainty],
    }


def _rankings(suppliers):
    return {
        "by_exceedance": [
            item["supplier"]
            for item in sorted(
                suppliers, key=lambda item: (-item["exceedance"], item["supplier"])
            )
        ],
        "naive_by_raw_rate": [
            item["supplier"]
            for item in sorted(
                suppliers, key=lambda item: (item["raw_rate"], item["supplier"])
            )
        ],
        "naive_by_total_defects": [
            item["supplier"]
            for item in sorted(
                suppliers, key=lambda item: (item["defects"], item["supplier"])
            )
        ],
    }


class QualityStudy:
    def __init__(self, journal):
        self.journal = journal
        with journal.transaction() as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS quality_studies(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL)"
            )

    def create(self, config):
        config = _config(config)
        with self.journal.transaction() as con:
            row = self.journal.one(
                "SELECT payload,digest FROM inspection_specs WHERE id=?",
                (config["spec"],),
            )
            if row is None:
                fail("missing", "inspection specification not found")
            spec = decode(row["payload"])
            batches = extract_evidence(
                self.journal, config["spec"], config["from"], config["to"]
            )
            random_batches, convenience_batches, counting = _finalize(
                batches, config["as_of"]
            )
            if not random_batches and not convenience_batches:
                fail("missing", "no sampling evidence in stratum")
            adjustments = applied_adjustments(batches, config["as_of"])
            payload = self._build(
                config,
                spec,
                row["digest"],
                batches,
                random_batches,
                convenience_batches,
                adjustments,
                counting,
            )
            digest = fingerprint(payload)
            old = self.journal.one(
                "SELECT digest FROM quality_studies WHERE id=?", (config["id"],)
            )
            if old:
                if old["digest"] != digest:
                    fail(
                        "conflict",
                        "study id refers to different evidence or configuration",
                    )
                return {"id": config["id"], "digest": digest, "reused": True}
            con.execute(
                "INSERT INTO quality_studies VALUES(?,?,?)",
                (config["id"], canonical(payload), digest),
            )
            self.journal.record(
                "quality_study",
                config["id"],
                {"spec": config["spec"], "digest": digest},
            )
        return {"id": config["id"], "digest": digest, "reused": False}

    def _build(
        self,
        config,
        spec,
        spec_digest,
        batches,
        random_batches,
        convenience_batches,
        adjustments,
        counting,
    ):
        level = config["interval_level"]
        tau = config["unacceptable_rate"]
        payload = {
            "schema": 1,
            "kind": STUDY_KIND,
            "id": config["id"],
            "config": config,
            "stratum": {
                "spec": config["spec"],
                "spec_digest": spec_digest,
                "material": spec["material"],
                "from": config["from"],
                "to": config["to"],
                "as_of": config["as_of"],
            },
            "evidence": {
                "random": {"inferable": True, "batches": random_batches},
                "convenience": {
                    "inferable": False,
                    "batches": convenience_batches,
                    "note": "convenience samples are listed separately and never generalized to lot defect rates",
                },
                "adjustments": adjustments,
                "counting": {
                    **counting,
                    "unit": "independent_physical_sample",
                    "rules": [
                        "weighed_quantities_never_become_samples",
                        "repeated_measurements_never_add_samples",
                        "rechecks_never_add_samples",
                        "withdrawn_judgments_never_count",
                    ],
                },
            },
            "notes": [
                "analysis supports next-period re-inspection scheduling and never freezes material automatically",
                "supplier size enters only as statistical evidence weight and is never used as a risk score",
            ],
        }
        payload["evidence"]["digest"] = fingerprint(
            {
                "random": random_batches,
                "convenience": convenience_batches,
                "adjustments": adjustments,
            }
        )
        if not random_batches:
            payload["fit"] = {
                "status": "not_inferable",
                "reason": "no representative random samples in stratum",
            }
            payload["suppliers"] = []
            payload["recommendations"] = {"high_risk": [], "high_uncertainty": []}
            payload["ranking"] = {
                "by_exceedance": [],
                "naive_by_raw_rate": [],
                "naive_by_total_defects": [],
            }
            payload["validation"] = {
                "status": "skipped",
                "reason": "not_inferable",
                "folds": [],
                "batches_scored": 0,
            }
            return payload
        fit = model.fit_hyperparameters(
            random_batches, config["prior"]["mode"], config["prior"]
        )
        prior = fit["prior"]
        grouped = model.group_by_supplier(random_batches)
        suppliers = []
        for supplier in sorted(grouped):
            own = grouped[supplier]
            defects = sum(batch["defects"] for batch in own)
            samples = sum(batch["samples"] for batch in own)
            shrunk = model.supplier_posterior(own, prior, level, tau)
            independent = model.independent_posterior(defects, samples, level)
            width = shrunk["interval"][1] - shrunk["interval"][0]
            categories = []
            if shrunk["exceedance"] >= config["risk_threshold"]:
                categories.append("high_risk")
            if width >= config["uncertainty_width"]:
                categories.append("high_uncertainty")
            suppliers.append(
                {
                    "supplier": supplier,
                    "batches": len(own),
                    "samples": samples,
                    "defects": defects,
                    "raw_rate": model.rounded(defects / samples),
                    "independent": {
                        "mean": model.rounded(independent["mean"]),
                        "interval": [
                            model.rounded(value) for value in independent["interval"]
                        ],
                    },
                    "shrunk": {
                        "mean": model.rounded(shrunk["mean"]),
                        "interval": [
                            model.rounded(value) for value in shrunk["interval"]
                        ],
                    },
                    "exceedance": model.rounded(shrunk["exceedance"]),
                    "interval_width": model.rounded(width),
                    "categories": categories,
                }
            )
        payload["fit"] = fit
        payload["suppliers"] = suppliers
        payload["recommendations"] = _recommendations(suppliers)
        payload["ranking"] = _rankings(suppliers)
        payload["validation"] = self._validation(batches, random_batches, config)
        return payload

    def _validation(self, batches, random_batches, config):
        level = config["interval_level"]

        def skipped(reason):
            return {
                "status": "skipped",
                "reason": reason,
                "folds": [],
                "batches_scored": 0,
            }

        if len(random_batches) < MIN_TRAIN_BATCHES + 1:
            return skipped("insufficient_batches")
        times = sorted({batch["received_at"] for batch in random_batches})
        if len(times) < 2:
            return skipped("insufficient_distinct_times")
        final_by_lot = {batch["lot"]: batch for batch in random_batches}
        sample_by_lot = {batch["lot"]: batch for batch in batches}
        folds = []
        for index in range(len(times) - 1):
            cutoff = times[index]
            train = []
            for lot, batch in sample_by_lot.items():
                if batch["received_at"] > cutoff:
                    continue
                counts = batch_counts(batch, "random", cutoff)
                if counts["samples"]:
                    train.append(
                        {
                            "supplier": batch["supplier"],
                            "samples": counts["samples"],
                            "defects": counts["defects"],
                        }
                    )
            if len(train) < MIN_TRAIN_BATCHES:
                continue
            test_lots = sorted(
                lot
                for lot, batch in sample_by_lot.items()
                if batch["received_at"] == times[index + 1] and lot in final_by_lot
            )
            if not test_lots:
                continue
            fit = model.fit_hyperparameters(
                train, config["prior"]["mode"], config["prior"]
            )
            prior = fit["prior"]
            grouped = model.group_by_supplier(train)
            for lot in test_lots:
                final = final_by_lot[lot]
                own = grouped.get(final["supplier"], [])
                eb_pmf = model.predictive_pmf(own, prior, final["samples"])
                train_defects = sum(batch["defects"] for batch in own)
                train_samples = sum(batch["samples"] for batch in own)
                in_pmf = model.independent_predictive_pmf(
                    train_defects, train_samples, final["samples"]
                )
                y = final["defects"]
                eb_interval = model.pmf_interval(eb_pmf, level)
                in_interval = model.pmf_interval(in_pmf, level)
                folds.append(
                    {
                        "cutoff": cutoff,
                        "lot": lot,
                        "supplier": final["supplier"],
                        "samples": final["samples"],
                        "defects": y,
                        "fit_status": fit["status"],
                        "training": {
                            "batches": len(train),
                            "samples": sum(batch["samples"] for batch in train),
                            "defects": sum(batch["defects"] for batch in train),
                        },
                        "empirical_bayes": {
                            "interval": eb_interval,
                            "covered": eb_interval[0] <= y <= eb_interval[1],
                            "log_score": model.rounded(math.log(eb_pmf[y])),
                        },
                        "independent": {
                            "interval": in_interval,
                            "covered": in_interval[0] <= y <= in_interval[1],
                            "log_score": model.rounded(math.log(in_pmf[y])),
                        },
                    }
                )
        if not folds:
            return skipped("no_scorable_folds")
        coverage_eb = sum(fold["empirical_bayes"]["covered"] for fold in folds) / len(
            folds
        )
        coverage_in = sum(fold["independent"]["covered"] for fold in folds) / len(folds)
        score_eb = sum(fold["empirical_bayes"]["log_score"] for fold in folds) / len(
            folds
        )
        score_in = sum(fold["independent"]["log_score"] for fold in folds) / len(folds)
        return {
            "status": "ok",
            "scheme": "rolling_origin_whole_batch_holdout",
            "min_train_batches": MIN_TRAIN_BATCHES,
            "batches_scored": len(folds),
            "coverage": {
                "empirical_bayes": model.rounded(coverage_eb),
                "independent": model.rounded(coverage_in),
            },
            "log_score": {
                "empirical_bayes": model.rounded(score_eb),
                "independent": model.rounded(score_in),
            },
            "folds": folds,
            "notes": [
                "each cutoff fits only batches received and judgments recorded by that cutoff",
                "held-out batches are scored against final counts known at study time",
            ],
        }

    def get(self, study_id):
        row = self.journal.one(
            "SELECT payload,digest FROM quality_studies WHERE id=?", (study_id,)
        )
        if row is None:
            fail("missing", "quality study not found")
        payload = decode(row["payload"])
        if fingerprint(payload) != row["digest"]:
            fail("integrity", "study digest mismatch")
        return {"payload": payload, "digest": row["digest"]}

    def list(self):
        return [
            {"id": row["id"], "digest": row["digest"]}
            for row in self.journal.rows(
                "SELECT id,digest FROM quality_studies ORDER BY id"
            )
        ]

    def export(self, study_id, path):
        study = self.get(study_id)
        return {
            **write_new(path, (canonical(study) + "\n").encode()),
            "digest": study["digest"],
        }

    def compare(self, first_id, second_id):
        first = self.get(first_id)["payload"]
        second = self.get(second_id)["payload"]
        if first["stratum"]["spec_digest"] != second["stratum"]["spec_digest"]:
            fail("input", "studies must share an inspection specification revision")
        return _compare(first, second)


def _compare(first, second):
    def ranks(payload):
        return {
            supplier: index + 1
            for index, supplier in enumerate(payload["ranking"]["by_exceedance"])
        }

    first_suppliers = {item["supplier"]: item for item in first["suppliers"]}
    second_suppliers = {item["supplier"]: item for item in second["suppliers"]}
    first_ranks = ranks(first)
    second_ranks = ranks(second)
    changes = []
    for supplier in sorted(set(first_suppliers) | set(second_suppliers)):
        before = first_suppliers.get(supplier)
        after = second_suppliers.get(supplier)
        changes.append(
            {
                "supplier": supplier,
                "rank_first": first_ranks.get(supplier),
                "rank_second": second_ranks.get(supplier),
                "exceedance_first": before["exceedance"] if before else None,
                "exceedance_second": after["exceedance"] if after else None,
                "shrunk_mean_first": before["shrunk"]["mean"] if before else None,
                "shrunk_mean_second": after["shrunk"]["mean"] if after else None,
            }
        )
    first_batches = {
        batch["lot"]: batch for batch in first["evidence"]["random"]["batches"]
    }
    second_batches = {
        batch["lot"]: batch for batch in second["evidence"]["random"]["batches"]
    }
    batches_changed = [
        {
            "lot": lot,
            "samples_first": first_batches[lot]["samples"],
            "samples_second": second_batches[lot]["samples"],
            "defects_first": first_batches[lot]["defects"],
            "defects_second": second_batches[lot]["defects"],
        }
        for lot in sorted(set(first_batches) & set(second_batches))
        if (first_batches[lot]["samples"], first_batches[lot]["defects"])
        != (second_batches[lot]["samples"], second_batches[lot]["defects"])
    ]

    def adjustments(payload):
        return {canonical(item) for item in payload["evidence"]["adjustments"]}

    added = [
        decode(item) for item in sorted(adjustments(second) - adjustments(first))
    ]
    return {
        "first": first["id"],
        "second": second["id"],
        "ranking_changes": changes,
        "evidence_changes": {
            "batches_added": sorted(set(second_batches) - set(first_batches)),
            "batches_removed": sorted(set(first_batches) - set(second_batches)),
            "batches_changed": batches_changed,
            "adjustments_added": added,
        },
        "note": "ranking differences trace to the listed evidence corrections frozen into each study",
    }


def verify_study(path):
    data = decode(read_file(path))
    if not isinstance(data, dict) or set(data) != {"payload", "digest"}:
        fail("input", "invalid study export envelope")
    payload = data["payload"]
    if fingerprint(payload) != data["digest"]:
        fail("integrity", "study fingerprint mismatch")
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != 1
        or payload.get("kind") != STUDY_KIND
    ):
        fail("input", "unsupported study export")
    return {
        "id": payload["id"],
        "digest": data["digest"],
        "suppliers": len(payload["suppliers"]),
    }
