# This file is part of LensKit.
# Copyright (C) 2018-2023 Boise State University.
# Copyright (C) 2023-2025 Drexel University.
# Licensed under the MIT license, see LICENSE.md for details.
# SPDX-License-Identifier: MIT

"""
User-based k-NN collaborative filtering.
"""

# pyright: basic
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
import pyarrow as pa
import scipy.sparse.linalg as spla
from pydantic import AliasChoices, BaseModel, Field, PositiveFloat, PositiveInt, field_validator
from scipy.sparse import csr_array
from typing_extensions import NamedTuple, Optional, override

from lenskit._accel import knn
from lenskit.data import Dataset, FeedbackType, ItemList, QueryInput, RecQuery
from lenskit.data.matrix import SparseRowArray
from lenskit.data.vocab import Vocabulary
from lenskit.diagnostics import DataWarning
from lenskit.logging import Stopwatch, get_logger
from lenskit.parallel.config import ensure_parallel_init
from lenskit.pipeline import Component
from lenskit.training import Trainable, TrainingOptions

_log = get_logger(__name__)


class UserKNNConfig(BaseModel, extra="forbid"):
    "Configuration for :class:`ItemKNNScorer`."

    max_nbrs: PositiveInt = Field(20, validation_alias=AliasChoices("max_nbrs", "nnbrs", "k"))
    """
    The maximum number of neighbors for scoring each item.
    """
    min_nbrs: PositiveInt = 1
    """
    The minimum number of neighbors for scoring each item.
    """
    min_sim: PositiveFloat = 1.0e-6
    """
    Minimum similarity threshold for considering a neighbor.  Must be positive;
    if less than the smallest 32-bit normal (:math:`1.175 \\times 10^{-38}`), is
    clamped to that value.
    """
    feedback: FeedbackType = "explicit"
    """
    The type of input data to use (explicit or implicit).  This affects data
    pre-processing and aggregation.
    """

    @field_validator("min_sim", mode="after")
    @staticmethod
    def clamp_min_sim(sim) -> float:
        return max(sim, float(np.finfo(np.float64).smallest_normal))

    @property
    def explicit(self) -> bool:
        """
        Query whether this is in explicit-feedback mode.
        """
        return self.feedback == "explicit"


class UserKNNScorer(Component[ItemList], Trainable):
    """
    User-user nearest-neighbor collaborative filtering with ratings. This
    user-user implementation is not terribly configurable; it hard-codes design
    decisions found to work well in the previous Java-based LensKit code.

    .. note::

        This component must be used with queries containing the user's history,
        either directly in the input or by wiring its query input to the output of a
        user history component (e.g., :class:`~lenskit.basic.UserTrainingHistoryLookup`).

    Stability:
        Caller
    """

    config: UserKNNConfig

    users: Vocabulary
    "The index of user IDs."
    items: Vocabulary
    "The index of item IDs."
    user_means: np.ndarray[tuple[int], np.dtype[np.float32]] | None
    "Mean rating for each known user."
    user_vectors: csr_array
    "Normalized rating matrix (CSR) to find neighbors at prediction time."
    user_ratings: SparseRowArray
    "Centered but un-normalized rating matrix (COO) to find neighbor ratings."

    @override
    def train(self, data: Dataset, options: TrainingOptions = TrainingOptions()):
        """
        "Train" a user-user CF model.  This memorizes the rating data in a format that is usable
        for future computations.

        Args:
            ratings(pandas.DataFrame): (user, item, rating) data for collaborative filtering.
        """
        if hasattr(self, "user_ratings_") and not options.retrain:
            return

        ensure_parallel_init()
        rmat = (
            data.interactions().matrix().scipy(attribute="rating" if self.config.explicit else None)
        ).astype(np.float32)

        rmat, means = self._center_ratings(rmat)
        normed = self._normalize_rows(rmat)

        self.user_vectors = normed
        self.user_ratings = SparseRowArray.from_scipy(rmat, values=self.config.explicit)
        self.users = data.users
        self.user_means = means
        self.items = data.items

    def _center_ratings(self, rmat: csr_array) -> tuple[csr_array, np.ndarray | None]:
        if self.config.explicit:
            counts = np.diff(rmat.indptr)
            sums = rmat.sum(axis=1)
            means = np.zeros(sums.shape, dtype=np.float32)
            # manual divide to avoid division by zero
            np.divide(sums, counts, out=means, where=counts > 0)
            rmat.data = rmat.data - np.repeat(means, counts)
            if np.allclose(rmat.data, 0.0):
                warnings.warn(
                    "Ratings seem to have the same value, centering is not recommended.",
                    DataWarning,
                )
            return rmat, means
        else:
            return rmat, None

    def _normalize_rows(self, rmat: csr_array) -> csr_array:
        norms = spla.norm(rmat, 2, axis=1)
        assert norms.shape == (rmat.shape[0],)
        # clamp small values to avoid divide by 0 (only appear when an entry is all 0)
        cmat = rmat / np.maximum(norms, np.finfo("f4").smallest_normal).reshape(-1, 1)
        assert cmat.shape == rmat.shape
        return cmat.tocsr()

    @override
    def __call__(self, query: QueryInput, items: ItemList) -> ItemList:
        """
        Compute predictions for a user and items.

        Args:
            user: the user ID
            items (array-like): the items to predict
            ratings (pandas.Series):
                the user's ratings (indexed by item id); if provided, will be used to
                recompute the user's bias at prediction time.

        Returns:
            pandas.Series: scores for the items, indexed by item id.
        """
        query = RecQuery.create(query)
        watch = Stopwatch()
        log = _log.bind(user_id=query.user_id, n_items=len(items))
        if len(items) == 0:
            log.debug("no candidate items, skipping")
            return ItemList(items, scores=np.nan)

        udata = self._get_user_data(query)
        if udata is None:
            log.debug("user has no ratings, skipping")
            return ItemList(items, scores=np.nan)

        uidx, ratings, umean = udata
        assert ratings.shape == (len(self.items),)  # ratings is a dense vector

        # now ratings has vbeen normalized to be a mean-centered unit vector
        # this means we can dot product to score neighbors
        # score the neighbors!
        nbr_sims = self.user_vectors @ ratings
        assert nbr_sims.shape == (len(self.users),)
        if uidx is not None:
            # zero out the self-similarity
            nbr_sims[uidx] = 0

        # get indices for these neighbors
        nbr_idxs = np.arange(len(self.users), dtype=np.int32)

        nbr_mask = nbr_sims >= self.config.min_sim

        kn_sims = nbr_sims[nbr_mask]
        kn_idxs = nbr_idxs[nbr_mask]
        if len(kn_sims) > 0:
            log.debug(
                "found %d candidate neighbors (of %d total), max sim %0.4f",
                len(kn_sims),
                len(self.users),
                np.max(kn_sims).item(),
            )
        else:
            log.debug("no candidate neighbors found, cannot score")
            return ItemList(items, scores=np.nan)

        assert not np.any(np.isnan(kn_sims))

        iidxs = items.numbers(vocabulary=self.items, missing="negative")

        ki_mask = iidxs >= 0
        usable_iidxs = iidxs[ki_mask]

        usable_iidxs = pa.array(usable_iidxs, pa.int32())
        kn_idxs = pa.array(kn_idxs, pa.int32())
        kn_sims = pa.array(kn_sims, pa.float32())

        if self.config.explicit:
            scores = knn.user_score_items_explicit(
                usable_iidxs,
                kn_idxs,
                kn_sims,
                self.user_ratings,
                self.config.max_nbrs,
                self.config.min_nbrs,
            )
        else:
            scores = knn.user_score_items_implicit(
                usable_iidxs,
                kn_idxs,
                kn_sims,
                self.user_ratings,
                self.config.max_nbrs,
                self.config.min_nbrs,
            )

        scores = scores.to_numpy(zero_copy_only=False, writable=True)
        scores += umean

        results = pd.Series(scores, index=items.ids()[ki_mask], name="prediction")
        results = results.reindex(items.ids())

        log.debug(
            "scored %d items in %s",
            results.notna().sum(),
            watch,
        )
        return ItemList(items, scores=results.values)  # type: ignore

    def _get_user_data(self, query: RecQuery) -> Optional[UserRatings]:
        "Get a user's data for user-user CF"

        index = self.users.number(query.user_id, missing=None)

        if query.user_items is None:
            if index is None:
                _log.warning("user %s has no ratings and none provided", query.user_id)
                return None

            index = int(index)
            row = self.user_vectors[[index], :].toarray()[0, :]
            if self.config.explicit:
                assert self.user_means is not None
                umean = self.user_means[index].item()
            else:
                umean = 0
            return UserRatings(index, row, umean)
        elif len(query.user_items) == 0:
            return None
        else:
            _log.debug("using provided item history")
            ratings = np.zeros(len(self.items), dtype=np.float32)
            ui_nos = query.user_items.numbers(missing="negative", vocabulary=self.items)
            ui_mask = ui_nos >= 0

            if self.config.explicit:
                urv = query.user_items.field("rating")
                if urv is None:
                    _log.warning("user %s has items but no ratings", query.user_id)
                    return None

                urv = np.require(urv, dtype=np.float32)
                umean = urv.mean().item()
                ratings[ui_nos[ui_mask]] = urv[ui_mask] - umean
            else:
                umean = 0
                ratings[ui_nos[ui_mask]] = 1.0

            return UserRatings(index, ratings, umean)


class UserRatings(NamedTuple):
    """
    Dense user ratings.
    """

    index: int | None
    ratings: np.ndarray[tuple[int], np.dtype[np.float32]]
    mean: float
