import math


def log_choose(n, k):
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def log_beta(a, b):
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def beta_binomial_logpmf(y, n, a, b):
    return log_choose(n, y) + log_beta(y + a, n - y + b) - log_beta(a, b)


def beta_logpdf(x, a, b):
    return (a - 1.0) * math.log(x) + (b - 1.0) * math.log1p(-x) - log_beta(a, b)


def logsumexp(values):
    peak = max(values)
    return peak + math.log(sum(math.exp(value - peak) for value in values))


def logit_grid(points, span):
    step = 2.0 * span / points
    grid = []
    for index in range(points):
        x = -span + (index + 0.5) * step
        if x >= 0.0:
            mu = 1.0 / (1.0 + math.exp(-x))
        else:
            mu = math.exp(x) / (1.0 + math.exp(x))
        grid.append((mu, mu * (1.0 - mu)))
    return grid


def grid_weights(logpdf, grid):
    logs = [logpdf(mu) + math.log(jacobian) for mu, jacobian in grid]
    total = logsumexp(logs)
    return [math.exp(value - total) for value in logs]


def grid_mean(grid, weights):
    return sum(mu * weight for (mu, _), weight in zip(grid, weights))


def grid_cdf(grid, weights, value):
    mus = [mu for mu, _ in grid]
    if value <= mus[0]:
        return 0.0
    if value >= mus[-1]:
        return 1.0
    cumulative = 0.0
    for index in range(len(mus) - 1):
        cumulative += weights[index]
        if value < mus[index + 1]:
            return cumulative + weights[index + 1] * (value - mus[index]) / (
                mus[index + 1] - mus[index]
            )
    return 1.0


def grid_quantile(grid, weights, q):
    mus = [mu for mu, _ in grid]
    cumulative = 0.0
    for index, weight in enumerate(weights):
        following = cumulative + weight
        if q <= following:
            if index == 0 or weight <= 0.0:
                return mus[index]
            return mus[index - 1] + (q - cumulative) / weight * (
                mus[index] - mus[index - 1]
            )
        cumulative = following
    return mus[-1]


def grid_interval(grid, weights, level):
    tail = (1.0 - level) / 2.0
    return [
        grid_quantile(grid, weights, tail),
        grid_quantile(grid, weights, 1.0 - tail),
    ]


def nelder_mead(function, start, step=0.3, max_iter=240, tolerance=1e-8):
    size = len(start)
    simplex = [list(start)]
    for index in range(size):
        point = list(start)
        point[index] += step
        simplex.append(point)
    values = [function(point) for point in simplex]
    converged = False
    for _ in range(max_iter):
        order = sorted(range(size + 1), key=lambda item: values[item])
        simplex = [simplex[item] for item in order]
        values = [values[item] for item in order]
        spread = max(abs(value - values[0]) for value in values)
        diameter = max(
            abs(simplex[item][axis] - simplex[0][axis])
            for item in range(1, size + 1)
            for axis in range(size)
        )
        if spread < tolerance * (1.0 + abs(values[0])) and diameter < 1e-6:
            converged = True
            break
        centroid = [
            sum(simplex[item][axis] for item in range(size)) / size
            for axis in range(size)
        ]
        worst = simplex[-1]
        reflected = [
            centroid[axis] + (centroid[axis] - worst[axis]) for axis in range(size)
        ]
        reflected_value = function(reflected)
        if reflected_value < values[-2]:
            if reflected_value < values[0]:
                expanded = [
                    centroid[axis] + 2.0 * (reflected[axis] - centroid[axis])
                    for axis in range(size)
                ]
                expanded_value = function(expanded)
                if expanded_value < reflected_value:
                    simplex[-1], values[-1] = expanded, expanded_value
                else:
                    simplex[-1], values[-1] = reflected, reflected_value
            else:
                simplex[-1], values[-1] = reflected, reflected_value
            continue
        if reflected_value < values[-1]:
            contracted = [
                centroid[axis] + 0.5 * (reflected[axis] - centroid[axis])
                for axis in range(size)
            ]
            contracted_value = function(contracted)
            if contracted_value <= reflected_value:
                simplex[-1], values[-1] = contracted, contracted_value
                continue
        else:
            contracted = [
                centroid[axis] + 0.5 * (worst[axis] - centroid[axis])
                for axis in range(size)
            ]
            contracted_value = function(contracted)
            if contracted_value < values[-1]:
                simplex[-1], values[-1] = contracted, contracted_value
                continue
        best = simplex[0]
        for item in range(1, size + 1):
            simplex[item] = [
                best[axis] + 0.5 * (simplex[item][axis] - best[axis])
                for axis in range(size)
            ]
            values[item] = function(simplex[item])
    return simplex[0], values[0], converged
