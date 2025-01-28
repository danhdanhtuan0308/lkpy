# This file is part of LensKit.
# Copyright (C) 2018-2023 Boise State University
# Copyright (C) 2023-2024 Drexel University
# Licensed under the MIT license, see LICENSE.md for details.
# SPDX-License-Identifier: MIT

"""
Code to import MovieLens data sets into LensKit.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias
from zipfile import ZipFile

import numpy as np
import pandas as pd

from lenskit.logging import get_logger

from .adapt import from_interactions_df
from .dataset import Dataset

_log = get_logger(__name__)

LOC: TypeAlias = Path | tuple[ZipFile, str]


@dataclass
class MLData:
    """
    Internal class representing an open ML data set.

    .. stability:: internal
    """

    version: str
    source: Path | ZipFile
    prefix: str = ""

    @staticmethod
    def version_impl(version: str) -> Callable[..., MLData]:
        if version == "ml-100k":
            return ML100KLoader
        elif re.match(r"^ml-10?m(100k)?$", version, re.IGNORECASE):
            return MLMLoader
        elif re.match(r"^ml-(\d+m|latest(-small)?)$", version, re.IGNORECASE):
            return MLModernLoader
        else:
            raise ValueError(f"unknown ML version {version}")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if isinstance(self.source, ZipFile):
            self.source.close()

    def open_file(self, name: str):
        if isinstance(self.source, Path):
            return open(self.source / (self.prefix + name), "r")
        else:
            return self.source.open(self.prefix + name)

    def ratings_df(self) -> pd.DataFrame:
        """
        Load the ratings data frame.
        """
        raise NotImplementedError()


class ML100KLoader(MLData):
    """
    Loader for the ML100K data set.
    """

    def ratings_df(self) -> pd.DataFrame:
        with self.open_file("u.data") as data:
            return pd.read_csv(
                data,
                sep="\t",
                header=None,
                names=["user_id", "item_id", "rating", "timestamp"],
                dtype={
                    "user_id": np.int32,
                    "item_id": np.int32,
                    "rating": np.float32,
                    "timestamp": np.int32,
                },
            )


class MLMLoader(MLData):
    """
    Loader for the ML 1M and 10M data sets.
    """

    def ratings_df(self):
        with self.open_file("ratings.dat") as data:
            return pd.read_csv(
                data,
                sep=":",
                header=None,
                names=["user_id", "_ui", "item_id", "_ir", "rating", "_rt", "timestamp"],
                usecols=[0, 2, 4, 6],
                dtype={
                    "user_id": np.int32,
                    "item_id": np.int32,
                    "rating": np.float32,
                    "timestamp": np.int32,
                },
            )


class MLModernLoader(MLData):
    """
    Loader for modern MovieLens data sets (20M and later).
    """

    def ratings_df(self):
        with self.open_file("ratings.csv") as data:
            return pd.read_csv(
                data,
                dtype={
                    "userId": np.int32,
                    "movieId": np.int32,
                    "rating": np.float32,
                    "timestamp": np.int64,
                },
            ).rename(columns={"userId": "user_id", "movieId": "item_id"})


def load_movielens(path: str | Path) -> Dataset:
    """
    Load a MovieLens dataset.  The appropriate MovieLens format is detected
    based on the file contents.

    Stability:
        Caller

    Args:
        path:
            The path to the dataset, either as an unpacked directory or a zip
            file.

    Returns:
        The dataset.
    """
    df = load_movielens_df(path)
    return from_interactions_df(df)


def load_movielens_df(path: str | Path) -> pd.DataFrame:
    """
    Load the ratings from a MovieLens dataset as a raw data frame.  The
    appropriate MovieLens format is detected based on the file contents.

    Stability:
        Caller

    Args:
        path:
            The path to the dataset, either as an unpacked directory or a zip
            file.

    Returns:
        The ratings, with columns ``user_id``, ``item_id``, ``rating``, and
        ``timestamp``.
    """
    with _ml_detect_and_open(path) as ml:
        return ml.ratings_df()


def _ml_detect_and_open(path: str | Path) -> MLData:
    loc = Path(path)
    version: str
    ctor: Callable[..., MLData]

    if loc.is_file() and loc.suffix == ".zip":
        log = _log.bind(zipfile=str(loc))
        log.debug("opening zip file")
        zf = ZipFile(loc, "r")
        try:
            infos = zf.infolist()
            first = infos[0]
            if not first.is_dir:
                log.error("first entry is not directory")
                raise RuntimeError("invalid ML zip file")

            log.debug("base dir filename %s", first.filename)
            dsm = re.match(r"^(ml-(?:\d+[MmKk]|latest|latest-small))", first.filename)
            if not dsm:
                log.error("invalid directory name %s", first.filename)
                raise RuntimeError("invalid ML zip file")

            version = dsm.group(1).lower()
            log.debug("found ML data set %s", version)
            ctor = MLData.version_impl(version)
            return ctor(version, zf, first.filename)
        except Exception as e:  # pragma nocover
            zf.close()
            raise e
    else:
        log = _log.bind(dir=str(loc))
        log.debug("loading from directory")
        dsm = re.match(r"^(ml-\d+[MmKk])", loc.name)
        if dsm:
            version = dsm.group(1)
            ctor = MLData.version_impl(dsm.group(1))
            _log.debug("inferred data set %s from dir name", version)
        else:
            _log.debug("checking contents for data type")
            if (loc / "u.data").exists():
                _log.debug("found u.data, interpreting as 100K")
                ctor = ML100KLoader
            elif (loc / "ratings.dat").exists():
                if (loc / "tags.dat").exists():
                    _log.debug("found ratings.dat and tags.dat, interpreting as 10M")
                    version = "ml-10m"
                else:
                    _log.debug("found ratings.dat but no tags, interpreting as 1M")
                    version = "ml-1m"
                ctor = MLMLoader
            elif (loc / "ratings.csv").exists():
                _log.debug("found ratings.csv, interpreting as modern (20M and later)")
                version = "ml-modern"
                ctor = MLModernLoader
            else:
                _log.error("could not detect MovieLens data")
                raise RuntimeError("invalid ML directory")

        return ctor(version, loc)
