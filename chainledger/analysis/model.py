import math
from .numerics import (
    beta_binomial_logpmf,
    beta_logpdf,
    grid_cdf,
    grid_interval,
    grid_mean,
    grid_weights,
    logit_grid,
    logsumexp,
    nelder_mead,
)

MEAN_BOUNDS = (0.05, 2000.0)
KAPPA_BOUNDS = (0.1, 100000.0)
CONCENTRATION_BOUNDARY = 3500.0
MEAN_BOUNDARY = 0.001
MINIMUM_BATCHES_FOR_ML = 4
UNIFORM_KAPPA = 10000.0
FIT_GRID = logit_grid(160, 11.0)
POSTERIOR_GRID = logit_grid(1500, 12.0)
PREDICTION_GRID = logit_grid(400, 12.0)


def rounded(value, digits=6):
    result = round(value, digits)
    return 0.0 if result == 0 else result


def group_by_supplier(batches):
    grouped = {}
    for batch in batches:
        grouped.setdefault(batch["supplier"], []).append(batch)
    return grouped


def _moments(grouped):
    rates = []
    for batches in grouped.values():
        defects = sum(batch["defects"] for batch in batches)
        samples = sum(batch["samples"] for batch in batches)
        rates.append(((defects + 0.5) / (samples + 1.0), samples))
    mean = sum(rate for rate, _ in rates) / len(rates)
    mean = min(max(mean, 1e-4), 1.0 - 1e-4)
    variance = sum((rate - mean) ** 2 for rate, _ in rates) / (len(rates) - 1)
    noise = sum(rate * (1.0 - rate) / samples for rate, samples in rates) / len(rates)
    excess = max(variance - noise, 1e-4)
    concentration = min(max(mean * (1.0 - mean) / excess - 1.0, 0.1), 4000.0)
    kappas = []
    for batches in grouped.values():
        if len(batches) < 2:
            continue
        qs = [(batch["defects"] + 0.5) / (batch["samples"] + 1.0) for batch in batches]
        qmean = sum(qs) / len(qs)
        qvar = sum((q - qmean) ** 2 for q in qs) / (len(qs) - 1)
        qnoise = sum(
            q * (1.0 - q) / batch["samples"] for q, batch in zip(qs, batches)
        ) / len(qs)
        qexcess = qvar - qnoise
        if qexcess > 1e-6:
            kappas.append(min(max(qmean * (1.0 - qmean) / qexcess - 1.0, 0.5), 1e4))
    kappa = sum(kappas) / len(kappas) if kappas else UNIFORM_KAPPA
    return mean * concentration, (1.0 - mean) * concentration, kappa


def _clamp(value, bounds):
    return min(max(value, bounds[0]), bounds[1])


def _objective(params, grouped, fit_kappa):
    a = math.exp(params[0])
    b = math.exp(params[1])
    kappa = math.exp(params[2]) if fit_kappa else UNIFORM_KAPPA
    violation = 0.0
    for value, bounds in ((a, MEAN_BOUNDS), (b, MEAN_BOUNDS), (kappa, KAPPA_BOUNDS)):
        if value < bounds[0]:
            violation += math.log(bounds[0]) - math.log(value)
        elif value > bounds[1]:
            violation += math.log(value) - math.log(bounds[1])
    if violation:
        return 1e90 + 1e90 * violation
    total = 0.0
    for batches in grouped.values():
        terms = []
        for mu, jacobian in FIT_GRID:
            alpha = mu * kappa
            beta = (1.0 - mu) * kappa
            likelihood = beta_logpdf(mu, a, b) + math.log(jacobian)
            for batch in batches:
                likelihood += beta_binomial_logpmf(
                    batch["defects"], batch["samples"], alpha, beta
                )
            terms.append(likelihood)
        total += logsumexp(terms)
    return -total


def _fit_empirical(grouped):
    notes = []
    total_batches = sum(len(batches) for batches in grouped.values())
    fit_kappa = any(len(batches) >= 2 for batches in grouped.values())
    if not fit_kappa:
        notes.append(
            "between-batch dispersion not identifiable; kappa fixed near binomial"
        )
    a, b, kappa = _moments(grouped)
    status = "moments"
    if total_batches >= MINIMUM_BATCHES_FOR_ML:
        start = [math.log(a), math.log(b)]
        if fit_kappa:
            start.append(math.log(kappa))
        best, _, converged = nelder_mead(
            lambda params: _objective(params, grouped, fit_kappa), start
        )
        if converged and all(math.isfinite(value) for value in best):
            a = math.exp(best[0])
            b = math.exp(best[1])
            if fit_kappa:
                kappa = math.exp(best[2])
            status = "ml"
        else:
            notes.append(
                "likelihood maximization did not converge; method of moments used"
            )
    else:
        notes.append("too few batches for likelihood fit; method of moments used")
    clamped = (
        _clamp(a, MEAN_BOUNDS),
        _clamp(b, MEAN_BOUNDS),
        _clamp(kappa, KAPPA_BOUNDS),
    )
    a, b, kappa = clamped
    boundaries = []
    for name, value, bounds in (
        ("a", a, MEAN_BOUNDS),
        ("b", b, MEAN_BOUNDS),
        ("kappa", kappa, KAPPA_BOUNDS),
    ):
        if math.log(value) - math.log(bounds[0]) < 0.02 or math.log(
            bounds[1]
        ) - math.log(value) < 0.02:
            boundaries.append(name + "_at_bound")
    if a + b >= CONCENTRATION_BOUNDARY:
        boundaries.append("concentrated_prior")
    mean = a / (a + b)
    if mean < MEAN_BOUNDARY or mean > 1.0 - MEAN_BOUNDARY:
        boundaries.append("extreme_prior_mean")
    return {
        "status": status,
        "a": a,
        "b": b,
        "kappa": kappa,
        "boundaries": boundaries,
        "notes": notes,
    }


def _summary(fit):
    return {
        "status": fit["status"],
        "a": rounded(fit["a"]),
        "b": rounded(fit["b"]),
        "kappa": rounded(fit["kappa"]),
        "boundaries": fit["boundaries"],
        "notes": fit["notes"],
    }


def fit_hyperparameters(batches, mode="empirical", external=None):
    grouped = group_by_supplier(batches)
    total_batches = sum(len(items) for items in grouped.values())
    total_samples = sum(
        batch["samples"] for items in grouped.values() for batch in items
    )
    total_defects = sum(
        batch["defects"] for items in grouped.values() for batch in items
    )
    boundaries = []
    if total_defects == 0:
        boundaries.append("zero_defects")
    elif total_defects == total_samples:
        boundaries.append("all_defective")
    empirical = _fit_empirical(grouped) if len(grouped) >= 2 else None
    result = {
        "groups": len(grouped),
        "batches": total_batches,
        "samples": total_samples,
        "defects": total_defects,
        "boundaries": boundaries,
        "notes": [],
    }
    if mode == "external":
        prior = {
            "a": external["a"],
            "b": external["b"],
            "kappa": external.get("kappa", UNIFORM_KAPPA),
            "source": "external_preregistered",
        }
        if "reference" in external:
            prior["reference"] = external["reference"]
        if "kappa" not in external:
            result["notes"].append(
                "external prior does not set between-batch dispersion; kappa defaulted"
            )
        if empirical is None:
            result["notes"].append("empirical prior not identifiable from current data")
        result["status"] = "external"
        result["prior"] = prior
        if empirical is not None:
            result["empirical"] = _summary(empirical)
        return result
    if empirical is None:
        result["status"] = "fallback"
        result["prior"] = {
            "a": 1.0,
            "b": 1.0,
            "kappa": UNIFORM_KAPPA,
            "source": "fallback_uniform",
        }
        result["notes"].append(
            "single supplier in stratum; uniform prior used and no shrinkage applied"
        )
        return result
    result["status"] = empirical["status"]
    result["prior"] = {
        "a": rounded(empirical["a"]),
        "b": rounded(empirical["b"]),
        "kappa": rounded(empirical["kappa"]),
        "source": "empirical_bayes",
    }
    result["boundaries"] = sorted(set(boundaries + empirical["boundaries"]))
    result["notes"] = empirical["notes"]
    return result


def _posterior_weights(batches, prior, grid):
    a, b, kappa = prior["a"], prior["b"], prior["kappa"]

    def logpdf(mu):
        alpha = mu * kappa
        beta = (1.0 - mu) * kappa
        value = beta_logpdf(mu, a, b)
        for batch in batches:
            value += beta_binomial_logpmf(batch["defects"], batch["samples"], alpha, beta)
        return value

    return grid_weights(logpdf, grid)


def supplier_posterior(batches, prior, level, tau):
    weights = _posterior_weights(batches, prior, POSTERIOR_GRID)
    return {
        "mean": grid_mean(POSTERIOR_GRID, weights),
        "interval": grid_interval(POSTERIOR_GRID, weights, level),
        "exceedance": 1.0 - grid_cdf(POSTERIOR_GRID, weights, tau),
    }


def independent_posterior(defects, samples, level):
    def logpdf(mu):
        return beta_logpdf(mu, defects + 1.0, samples - defects + 1.0)

    weights = grid_weights(logpdf, POSTERIOR_GRID)
    return {
        "mean": grid_mean(POSTERIOR_GRID, weights),
        "interval": grid_interval(POSTERIOR_GRID, weights, level),
    }


def _normalized(pmf):
    floored = [max(value, 1e-300) for value in pmf]
    total = sum(floored)
    return [value / total for value in floored]


def predictive_pmf(batches, prior, samples):
    weights = _posterior_weights(batches, prior, PREDICTION_GRID)
    kappa = prior["kappa"]
    pmf = []
    for y in range(samples + 1):
        mass = 0.0
        for (mu, _), weight in zip(PREDICTION_GRID, weights):
            mass += weight * math.exp(
                beta_binomial_logpmf(y, samples, mu * kappa, (1.0 - mu) * kappa)
            )
        pmf.append(mass)
    return _normalized(pmf)


def independent_predictive_pmf(defects, samples, n):
    if samples <= 0:
        return [1.0 / (n + 1)] * (n + 1)
    pmf = [
        math.exp(beta_binomial_logpmf(y, n, defects + 1.0, samples - defects + 1.0))
        for y in range(n + 1)
    ]
    return _normalized(pmf)


def pmf_interval(pmf, level):
    tail = (1.0 - level) / 2.0
    cumulative = 0.0
    low = 0
    high = len(pmf) - 1
    for index, mass in enumerate(pmf):
        cumulative += mass
        if cumulative >= tail:
            low = index
            break
    cumulative = 0.0
    for index, mass in enumerate(pmf):
        cumulative += mass
        if cumulative >= 1.0 - tail:
            high = index
            break
    return [low, high]
