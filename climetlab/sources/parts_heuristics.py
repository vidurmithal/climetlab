# (C) Copyright 2020 ECMWF.
#
# This software is licensed under the terms of the Apache Licence Version 2.0
# which can be obtained at http://www.apache.org/licenses/LICENSE-2.0.
# In applying this licence, ECMWF does not waive the privileges and immunities
# granted to it by virtue of its status as an intergovernmental organisation
# nor does it submit to any jurisdiction.
#

import logging
import re
from collections import namedtuple

LOG = logging.getLogger(__name__)


Part = namedtuple("Part", ["offset", "length"])


def round_down(a, b):
    return (a // b) * b


def round_up(a, b):
    return ((a + b - 1) // b) * b


def _positions(parts, blocks):

    i = 0
    positions = []
    block_offset, block_length = blocks[i]
    for offset, length in parts:
        while offset > block_offset + block_length:
            i += 1
            block_offset, block_length = blocks[i]
        start = i
        while offset + length > block_offset + block_length:
            i += 1
            block_offset, block_length = blocks[i]
        end = i
        # Sanity check: assert that each parts is contain in a rounded part
        assert start == end
        positions.append(offset - blocks[i][0] + sum(blocks[j][1] for j in range(i)))

    return positions


class HierarchicalClustering:
    def __init__(self, min_clusters=5):
        self.min_clusters = min_clusters

    def __call__(self, parts):
        clusters = [Part(offset, length) for offset, length in parts]

        while len(clusters) > self.min_clusters:
            min_dist = min(
                clusters[i].offset - clusters[i - 1].offset + clusters[i - 1].length
                for i in range(1, len(clusters))
            )
            i = 1
            while i < len(clusters):
                d = clusters[i].offset - clusters[i - 1].offset + clusters[i - 1].length
                if d <= min_dist:
                    clusters[i - 1] = Part(
                        clusters[i - 1].offset,
                        clusters[i].offset
                        + clusters[i].length
                        - clusters[i - 1].offset,
                    )
                    clusters.pop(i)
                else:
                    i += 1

        return clusters, _positions(parts, clusters)


class BlockGrouping:
    def __init__(self, block_size):
        self.block_size = block_size

    def __call__(self, parts):
        blocks = []
        last_block_offset = -1
        last_offset = 0

        for offset, length in parts:

            assert offset >= last_offset

            block_offset = round_down(offset, self.block_size)
            block_length = round_up(offset + length, self.block_size) - block_offset

            if block_offset <= last_block_offset:
                prev_offset, prev_length = blocks.pop()
                end_offset = block_offset + block_length
                prev_end_offset = prev_offset + prev_length
                block_offset = min(block_offset, prev_offset)
                assert block_offset == prev_offset
                block_length = max(end_offset, prev_end_offset) - block_offset

            blocks.append((block_offset, block_length))

            last_block_offset = block_offset + block_length
            last_offset = offset + length

        return blocks, _positions(parts, blocks)


class Automatic:
    def __call__(self, parts):
        smallest = min(x[1] for x in parts)
        transfer_size = round_up(max(x[1] for x in parts), 1024)

        while transfer_size >= smallest:
            blocks, positions = BlockGrouping(transfer_size)(parts)
            transfer_size //= 2

        return blocks, positions


HEURISTICS = {
    "auto": Automatic,
    "cluster": HierarchicalClustering,
    "blocked": BlockGrouping,
}


def parts_heuristics(name):

    if isinstance(name, int):
        return BlockGrouping(name)

    if "(" in name:
        m = re.match(r"(.+)\((.+)\)", name)
        name = m.group(1)
        args = [int(a) for a in m.group(2).split(",")]
    else:
        args = []

    return HEURISTICS[name](*args)
