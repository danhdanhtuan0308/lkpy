# This file is part of LensKit.
# Copyright (C) 2018-2023 Boise State University
# Copyright (C) 2023-2025 Drexel University
# Licensed under the MIT license, see LICENSE.md for details.
# SPDX-License-Identifier: MIT

import pickle
from pathlib import Path

import click

import lenskit.operations as ops
from lenskit.data import Dataset
from lenskit.logging import get_logger

_log = get_logger(__name__)


@click.command("recommend")
@click.option(
    "-o",
    "--output",
    "out_file",
    metavar="FILE",
    help="Output file for recommendations.",
)
@click.option("-n", "--list-length", type=int, help="Recommendation list length.")
@click.option("-d", "--dataset", metavar="DATA", type=Path, help="Use dataset DATA.")
@click.argument("PIPE_FILE", type=Path)
@click.argument("USERS", nargs=-1)
def recommend(
    out_file: Path,
    list_length: int | None,
    dataset: Path | None,
    pipe_file: Path,
    users: list,
):
    """
    Generate recommendations from a serialized recommendation pipeline.
    """
    _log.warning("the recommend CLI is experimental and may change without notice")

    _log.info("loading pipeline", file=str(pipe_file))
    with open(pipe_file, "rb") as pf:
        pipe = pickle.load(pf)
    log = _log.bind(name=pipe.name)

    if dataset is not None:
        data = Dataset.load(dataset)
        log = log.bind(data=data.name)
    else:
        data = None

    for user in users:
        ulog = log.bind(user=user)
        ulog.debug("generating single-user recommendations")
        recs = ops.recommend(pipe, user, list_length)
        ulog.info("recommended for user", length=len(recs))

        titles = None
        if data is not None:
            items = data.entities("item")
            if "title" in items.attributes:
                titles = items.select(ids=recs.ids()).attribute("title").pandas()

        for item in recs.ids():
            if titles is not None:
                print("item {}: {}".format(item, titles.loc[item]))
            else:
                print("item {}".format(item))
