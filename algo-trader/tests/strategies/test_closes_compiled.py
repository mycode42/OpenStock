import math
import random

from strategies import _indicators_cy as cy
from strategies import indicators


def test_compiled_closes_matches_the_python_version_on_edge_cases():
    rng = random.Random(7)
    cases = [[], [{"c": 1.5}], [{"c": "2.5"}, {"c": None}, {}, {"c": math.nan}, {"c": -3}, {"c": 0}, {"c": math.inf}, {"c": "abc"}, {"c": 1e308}]]
    for _ in range(200):
        cases.append([{"c": rng.choice([rng.uniform(-5, 500), rng.randint(-3, 3), math.nan, "7.25", None])} for _ in range(rng.randint(0, 120))])
    for bars in cases:
        assert indicators._closes_py(bars) == cy.closes(bars)
        assert indicators.closes(bars) == indicators._closes_py(bars)
