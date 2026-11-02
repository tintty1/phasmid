"""BrowserForge's fingerprint network, sampled with a per-decision RNG.

browserforge draws with the module-global ``random.random()``. Every draw here
swaps that for ``random.Random(sha256(seed + ":" + decision))`` for the length
of one call, so a seed gives the same sample byte for byte and the global
``random`` state is never read or advanced.

Restricted draws renormalize over the accepted values before sampling.
browserforge's own ``sample_according_to_restrictions`` does not: when the
anchor falls past the accepted mass it returns the first candidate, so a
filter that keeps 20% of a node's mass returns its first value about 80% of
the time.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import cache
from importlib.metadata import version
from typing import Any

from apify_fingerprint_datapoints import get_fingerprint_network
from browserforge import bayesian_network
from browserforge.bayesian_network import BayesianNetwork

# browserforge's packing of object and missing values (FingerprintGenerator.generate).
_STRINGIFIED = "*STRINGIFIED*"
_MISSING = "*MISSING_VALUE*"


@cache
def network() -> BayesianNetwork:
    return BayesianNetwork(get_fingerprint_network())


@cache
def provenance() -> str:
    """Pinned versions and the hash of the network the samples come from."""
    digest = hashlib.sha256(get_fingerprint_network().read_bytes()).hexdigest()
    return (
        f"browserforge {version('browserforge')}, apify_fingerprint_datapoints "
        f"{version('apify_fingerprint_datapoints')}, fingerprint network sha256 {digest}"
    )


def unpack(raw: Any) -> Any:
    if raw == _MISSING:
        return None
    if isinstance(raw, str) and raw.startswith(_STRINGIFIED):
        return json.loads(raw[len(_STRINGIFIED):])
    return raw


def decision_rng(seed: str, decision: str) -> random.Random:
    return random.Random(int.from_bytes(hashlib.sha256(f"{seed}:{decision}".encode()).digest(), "big"))


@contextmanager
def _browserforge_rng(rng: random.Random) -> Iterator[None]:
    # bayesian_network calls the module ``random`` it imported; a Random instance has the same random().
    saved = getattr(bayesian_network, "random")
    setattr(bayesian_network, "random", rng)
    try:
        yield
    finally:
        setattr(bayesian_network, "random", saved)


def user_agents() -> dict[str, float]:
    """The root node: every user-agent string with its probability."""
    return network().nodes_by_name["userAgent"].get_probabilities_given_known_values({})


def draw(seed: str, decision: str, node_name: str, parents: dict[str, str], accept: Callable[[Any], bool]) -> Any:
    """One weighted draw from ``node_name`` given its parents, over the values
    ``accept`` keeps. Returns the unpacked value, or None when nothing passes."""
    node = network().nodes_by_name[node_name]
    table = node.get_probabilities_given_known_values(parents)
    candidates = sorted(v for v in table if accept(unpack(v)))
    mass = sum(table[v] for v in candidates)
    if not candidates or mass <= 0:
        return None
    weights = {v: table[v] / mass for v in candidates}
    with _browserforge_rng(decision_rng(seed, decision)):
        return unpack(node.sample_random_value_from_possibilities(candidates, weights))
