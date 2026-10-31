"""
backend/statistiques_simples.py

Petites fonctions statistiques SANS dépendance (ni numpy, ni scipy), pour l'analyse territoriale.
Chacune est simple, déterministe et validée contre scipy (valeurs identiques). Les noms et les
docstrings expliquent en clair ce que mesure chaque outil.
"""
from __future__ import annotations

import math
import random
from typing import Callable, Sequence


def moyenne(v: Sequence[float]) -> float | None:
    return sum(v) / len(v) if v else None


def quantile(v: Sequence[float], q: float) -> float | None:
    """Quantile par interpolation linéaire (même définition que percentile_cont de PostgreSQL)."""
    if not v:
        return None
    s = sorted(v)
    pos = (len(s) - 1) * q
    bas, haut = int(math.floor(pos)), int(math.ceil(pos))
    return s[bas] + (s[haut] - s[bas]) * (pos - bas)


def mediane(v: Sequence[float]) -> float | None:
    return quantile(v, 0.5)


def mad(v: Sequence[float]) -> float | None:
    """Écart absolu médian : la « dispersion normale » d'une série, insensible aux valeurs extrêmes."""
    m = mediane(v)
    return None if m is None else mediane([abs(x - m) for x in v])


def zscores_robustes(v: Sequence[float]) -> list[float]:
    """Combien d'« écarts normaux » chaque valeur est-elle éloignée de la médiane ? (> 3,5 = atypique)."""
    m, d = mediane(v), mad(v)
    if m is None or not d:
        return [0.0 for _ in v]
    return [0.6745 * (x - m) / d for x in v]


def rangs(v: Sequence[float]) -> list[float]:
    """Rangs moyens (les ex æquo se partagent la moyenne de leurs rangs)."""
    ordre = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    i = 0
    while i < len(ordre):
        j = i
        while j + 1 < len(ordre) and v[ordre[j + 1]] == v[ordre[i]]:
            j += 1
        moy = (i + j) / 2 + 1
        for k in range(i, j + 1):
            r[ordre[k]] = moy
        i = j + 1
    return r


def rang_percentile(v: Sequence[float]) -> list[float]:
    """Position de chaque valeur dans la série, de 0 (la plus basse) à 100 (la plus haute)."""
    n = len(v)
    if n <= 1:
        return [50.0 for _ in v]
    return [100.0 * (r - 1) / (n - 1) for r in rangs(v)]


def gini(v: Sequence[float]) -> float | None:
    """0 = tout est également réparti ; 1 = tout est concentré en un seul endroit."""
    s = sorted(x for x in v if x is not None)
    n, total = len(s), sum(s)
    if n == 0 or total <= 0:
        return None
    cumul = sum((i + 1) * x for i, x in enumerate(s))
    return (2 * cumul) / (n * total) - (n + 1) / n


def courbe_lorenz(v: Sequence[float]) -> list[tuple[float, float]]:
    """Points (part cumulée des territoires, part cumulée des lieux) du plus petit au plus gros."""
    s = sorted(v)
    total = sum(s)
    pts, cumul = [(0.0, 0.0)], 0.0
    for i, x in enumerate(s, start=1):
        cumul += x
        pts.append((i / len(s), cumul / total if total else 0.0))
    return pts


def hhi(parts: Sequence[float]) -> float | None:
    """Indice de Herfindahl-Hirschman (0 à 10 000). Mesure la dépendance à quelques acteurs."""
    total = sum(parts)
    if total <= 0:
        return None
    return sum((100.0 * p / total) ** 2 for p in parts)


def part_des_premiers(v: Sequence[float], k: int) -> float | None:
    total = sum(v)
    return None if total <= 0 else sum(sorted(v, reverse=True)[:k]) / total


def diversite_shannon(effectifs: Sequence[float]) -> float | None:
    """Équitabilité de Pielou (0 à 1) : 1 = toutes les catégories pèsent autant."""
    e = [x for x in effectifs if x > 0]
    if len(e) <= 1:
        return 0.0 if e else None
    total = sum(e)
    h = -sum((x / total) * math.log(x / total) for x in e)
    return h / math.log(len(e))


def intervalle_wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """Fourchette plausible (95 %) d'une proportion observée sur un petit échantillon."""
    if n <= 0:
        return None
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    marge = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, centre - marge), min(1.0, centre + marge)


def intervalle_bootstrap(v: Sequence[float], stat: Callable = mediane, n_rep: int = 1000, graine: int = 12345,
                         niveau: float = 0.95) -> tuple[float, float] | None:
    """Fourchette plausible d'une statistique (médiane par défaut) : on « rejoue » l'échantillon 1 000 fois."""
    if len(v) < 3:
        return None
    rng = random.Random(graine)
    n = len(v)
    vals = sorted(stat([v[rng.randrange(n)] for _ in range(n)]) for _ in range(n_rep))
    a = (1 - niveau) / 2
    return vals[int(a * n_rep)], vals[min(n_rep - 1, int((1 - a) * n_rep))]


def _gamma_inc_q(a: float, x: float) -> float:
    """Q(a, x) : fonction gamma incomplète régularisée supérieure."""
    if x <= 0:
        return 1.0
    gln = math.lgamma(a)
    if x < a + 1:
        ap, somme, delta = a, 1.0 / a, 1.0 / a
        for _ in range(500):
            ap += 1
            delta *= x / ap
            somme += delta
            if abs(delta) < abs(somme) * 1e-14:
                break
        return 1.0 - somme * math.exp(-x + a * math.log(x) - gln)
    b = x + 1 - a
    c, d = 1e300, 1 / b
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = 1e-300 if abs(d) < 1e-300 else d
        c = b + an / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-14:
            break
    return math.exp(-x + a * math.log(x) - gln) * h


def p_khi2(h: float, ddl: int) -> float:
    return 1.0 if ddl <= 0 else _gamma_inc_q(ddl / 2.0, h / 2.0)


def _beta_cf(a: float, b: float, x: float) -> float:
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1e-300 if abs(d) < 1e-300 else d
    d = 1 / d
    h = d
    for m in range(1, 500):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1 + aa * d; d = 1e-300 if abs(d) < 1e-300 else d
        c = 1 + aa / c; c = 1e-300 if abs(c) < 1e-300 else c
        d = 1 / d; h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1 + aa * d; d = 1e-300 if abs(d) < 1e-300 else d
        c = 1 + aa / c; c = 1e-300 if abs(c) < 1e-300 else c
        d = 1 / d; delta = d * c; h *= delta
        if abs(delta - 1) < 1e-14:
            break
    return h


def _beta_inc(a: float, b: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x))
    if x < (a + 1) / (a + b + 2):
        return bt * _beta_cf(a, b, x) / a
    return 1 - bt * _beta_cf(b, a, 1 - x) / b


def p_student_deux_cotes(t: float, ddl: float) -> float:
    return _beta_inc(ddl / 2.0, 0.5, ddl / (ddl + t * t))


def kruskal_wallis(groupes: Sequence[Sequence[float]]) -> dict | None:
    """Les groupes (ex. départements) diffèrent-ils vraiment ? → p-value et taille d'effet (epsilon²)."""
    groupes = [list(g) for g in groupes if len(g) > 0]
    k = len(groupes)
    toutes = [x for g in groupes for x in g]
    n = len(toutes)
    if k < 2 or n <= k:
        return None
    r = rangs(toutes)
    somme, i = 0.0, 0
    for g in groupes:
        rg = r[i:i + len(g)]
        i += len(g)
        somme += sum(rg) ** 2 / len(g)
    h = 12.0 / (n * (n + 1)) * somme - 3 * (n + 1)
    comptes: dict[float, int] = {}
    for x in toutes:
        comptes[x] = comptes.get(x, 0) + 1
    corr = 1 - sum(t ** 3 - t for t in comptes.values()) / (n ** 3 - n)
    if corr <= 0:
        return None
    h /= corr
    return {"H": h, "ddl": k - 1, "p": p_khi2(h, k - 1), "epsilon2": max(0.0, h * (n + 1) / (n * n - 1)), "n": n, "k": k}


def spearman(x: Sequence[float], y: Sequence[float]) -> dict | None:
    """Les deux séries évoluent-elles ensemble ? rho de -1 à +1, avec sa p-value."""
    n = len(x)
    if n < 4 or n != len(y):
        return None
    rx, ry = rangs(x), rangs(y)
    mx, my = sum(rx) / n, sum(ry) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx, syy = sum((a - mx) ** 2 for a in rx), sum((b - my) ** 2 for b in ry)
    if sxx == 0 or syy == 0:
        return None
    rho = sxy / math.sqrt(sxx * syy)
    p = 0.0 if abs(rho) >= 1 else p_student_deux_cotes(rho * math.sqrt((n - 2) / (1 - rho * rho)), n - 2)
    return {"rho": rho, "p": p, "n": n}


def jaccard(a: Sequence, b: Sequence) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb) if (sa | sb) else 1.0