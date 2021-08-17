#!/usr/bin/env python
#
# (C) Copyright 2021- ECMWF.
#
# This software is licensed under the terms of the Apache Licence Version 2.0
# which can be obtained at http://www.apache.org/licenses/LICENSE-2.0.
# In applying this licence, ECMWF does not waive the privileges and immunities
# granted to it by virtue of its status as an intergovernmental organisation nor
# does it submit to any jurisdiction.
#
import itertools
import logging

from climetlab.utils.humanize import did_you_mean

LOG = logging.getLogger(__name__)


VOCABULARIES = {}

SYNONYMS = (
    (("mars", "2t"), ("cf", "t2m")),
    (("mars", "ci"), ("cf", "siconc")),
)


class Vocabulary:
    def __init__(self, name):
        self.name = name
        self.words = set()
        self.aliases = {}

    def add(self, word, *aliases):
        self.words.add(word)
        for a in aliases:
            self.aliases[a] = word

    def lookup(self, word):
        w = self.aliases.get(word, word)

        if w in self.words:
            return w

        return None

    def normalise(self, word):

        w = self.lookup(word)
        if w is not None:
            return w

        #  For now....
        for synonyms in SYNONYMS:
            matches = [s for s in synonyms if s[0] != self.name and s[1]==word]
            if not matches:
                continue
            assert len(matches) == 1, f"Too many synonyms {matches}"
            for s in synonyms:
                if s[0] == self.name:
                    return s[1]



        correction = did_you_mean(
            word,
            itertools.chain(
                self.words,
                self.aliases.keys(),
            ),
        )
        if correction is not None:
            LOG.warning(
                "Cannot find '%s' in %s vocabulary, did you mean '%s'?",
                word,
                self.name,
                correction,
            )

        return word


mars = Vocabulary("mars")
mars.add("2t")
mars.add("tp")
mars.add("ci")
VOCABULARIES["mars"] = mars

cf = Vocabulary("cf")
cf.add("t2m")
cf.add("tp")
cf.add("siconc")
VOCABULARIES["cf"] = cf
