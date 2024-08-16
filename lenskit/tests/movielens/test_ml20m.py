# This file is part of LensKit.
# Copyright (C) 2018-2023 Boise State University
# Copyright (C) 2023-2024 Drexel University
# Licensed under the MIT license, see LICENSE.md for details.
# SPDX-License-Identifier: MIT

"""
Tests on the ML-20M data set.
"""

import logging
from pathlib import Path

import pytest

from lenskit import batch
from lenskit.algorithms import Recommender
from lenskit.algorithms.basic import PopScore
from lenskit.data import Dataset, from_interactions_df, load_movielens

_log = logging.getLogger(__name__)

_ml_path = Path("data/ml-20m")


@pytest.fixture(scope="module")
def ml20m():
    if _ml_path.exists():
        return load_movielens(_ml_path)
    else:
        pytest.skip("ML-20M not available")


@pytest.mark.slow
@pytest.mark.realdata
@pytest.mark.parametrize("n_jobs", [1, 2])
def test_pop_recommend(ml20m: Dataset, rng, n_jobs):
    users = rng.choice(ml20m.users.ids(), 10000, replace=False)
    algo = PopScore()
    algo = Recommender.adapt(algo)
    _log.info("training %s", algo)
    algo.fit(ml20m)
    _log.info("recommending with %s", algo)
    recs = batch.recommend(algo, users, 10, n_jobs=n_jobs)

    assert recs["user"].nunique() == 10000
