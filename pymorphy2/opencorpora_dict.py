# -*- coding: utf-8 -*-
"""
Module with utilities for converting OpenCorpora dictionaries
to pymorphy2 compact formats.
"""
from __future__ import absolute_import
import codecs
import os
import logging
import collections
import json
import re
import itertools
import copy

try:
    import cPickle as pickle
except ImportError:
    import pickle

from . import data

logger = logging.getLogger(__name__)

def _full_lemmas_from_xml(filename):
    """
    Iterator over "full" lemmas in OpenCorpora dictionary file (in .xml format).
    """
    from lxml import etree

    def _tags(elem):
        return ",".join(g.get('v') for g in elem.findall('g'))

    def _cleanup(elem):
        elem.clear()
        while elem.getprevious() is not None:
            del elem.getparent()[0]

    for ev, elem in etree.iterparse(filename, tag='lemma'):
        lemma = []

        base_info = elem.findall('l')
        assert len(base_info) == 1
        base_tags = _tags(base_info[0])

        for form_elem in elem.findall('f'):
            tags = _tags(form_elem)
            form = form_elem.get('t').upper()
            lemma.append(
                (form, " ".join([base_tags, tags]).strip())
            )

        yield lemma
        _cleanup(elem)


def _full_lemmas_from_txt(filename):
    """
    Iterator over "full" lemmas in OpenCorpora dictionary file (in .txt format).
    """
    with codecs.open(filename, 'rb', 'utf8') as f:
        it = iter(f)

        while True:
            try:
                lemma = []
                line = next(it).strip()
                lemma_id = int(line)
                while line:
                    line = next(it).strip()
                    if line:
                        parts = line.split(None, 1)
                        if len(parts) == 2:
                            form, tag = parts
                        else:
                            form, tag = parts, ''
                        lemma.append((form, tag))

                yield lemma

            except StopIteration:
                break

def _to_paradigm(lemma):
    """
    Extracts (stem, paradigm) pair from lemma list.
    Paradigm is a list of suffixes with associated gram. tags and prefixes.
    """
    forms, tags = list(zip(*lemma))
    prefixes = [''] * len(tags)

    stem = os.path.commonprefix(forms)

    if stem == "":
        for prefix in data.POSSIBLE_PREFIXES:
            without_prefixes = [
                form[len(prefix):] if form.startswith(prefix) else form
                for form in forms
            ]
            new_stem = os.path.commonprefix(without_prefixes)
            if new_stem:
                prefixes = [prefix if form.startswith(prefix) else '' for form in forms]
                stem = new_stem
                break

    suffixes = (
        form[len(pref)+len(stem):]
        for form, pref in zip(forms, prefixes)
    )

    return stem, tuple(zip(suffixes, tags, prefixes))

def _gram_structures(filename):
    """
    Returns compacted dictionary data.
    """
    gramtab = []
    paradigms = []
    words = []

    seen_tags = dict()
    seen_paradigms = dict()

    logger.debug("%20s %15s %15s %15s %15s", "stem", "len(gramtab)", "len(words)", "len(paradigms)", "len(suffixes)")

    for index, lemma in enumerate(_full_lemmas_from_xml(filename)):
        stem, paradigm = _to_paradigm(lemma)

        # build gramtab
        for suff, tag, pref in paradigm:
            if tag not in seen_tags:
                seen_tags[tag] = len(gramtab)
                gramtab.append(tag)

        # build paradigm index
        if paradigm not in seen_paradigms:
            seen_paradigms[paradigm] = len(paradigms)
            paradigms.append(
                tuple([(suff, seen_tags[tag], pref) for suff, tag, pref in paradigm])
            )

        para_id = seen_paradigms[paradigm]

        for idx, (suff, tag, pref) in enumerate(paradigm):
            form = pref+stem+suff
            words.append(
                (form, (para_id, idx))
            )

        if not (index % 10000):
            logger.debug("%20s %15s %15s %15s %15s",
                stem, len(gramtab), len(words), len(paradigms), 0)


    logger.debug('building data structures..')
    words_dawg = data.WordsDawg(words)

    return tuple(gramtab), paradigms, words_dawg


def _get_word_parses(filename):
    word_parses = collections.defaultdict(list) # word -> possible tags

    logger.debug("%10s %20s", "lemma #", "result size")
    for index, lemma in enumerate(_full_lemmas_from_txt(filename)):
        for word, tag in lemma:
            word_parses[word].append(tag)

        if not index % 10000:
            logger.debug('%10s %20s', index, len(word_parses))

    return word_parses


def _add_ee_parses(word_parses):

    def combinations_of_all_lengths(it):
        return itertools.chain(
            *(itertools.combinations(it, num+1) for num in range(len(it)))
        )

    def replace_chars(word, positions, replacement):
        word_list = list(word)
        for pos in positions:
            word_list[pos] = replacement
        return "".join(word_list)

    def missing_umlaut_variants(word):
        umlaut_positions = [m.start() for m in re.finditer('Ё', word, re.U)]
        for positions in combinations_of_all_lengths(umlaut_positions):
            yield replace_chars(word, positions, 'Е')


    _word_parses = copy.deepcopy(word_parses)

    for word in word_parses:
        parses = word_parses[word]

        for word_variant in missing_umlaut_variants(word):
            _word_parses[word_variant].extend(parses)

    return _word_parses


def _get_test_suite(word_parses, word_limit=100):
    """
    Limits word_parses to ``word_limit`` words per tag.
    """
    gramtab = collections.Counter() # tagset -> number of stored items
    result = list()
    for word in word_parses:
        parses = word_parses[word]
        gramtab.update(parses)
        if any(gramtab[tag] < word_limit for tag in parses):
            result.append((word, parses))

    return result


def _save_test_suite(path, suite):
    with open(path, 'wb') as f:
        for word, parses in suite:
            txt = "|".join([word]+parses) +'\n'
            f.write(txt.encode('utf8'))


def to_test_suite(opencorpora_txt_path, out_path, word_limit=100):
    """
    Extracts test data from OpenCorpora .txt dictionary (at least
    ``word_limit`` words for each distinct gram. tag) and saves it to a file.
    """
    logger.debug('loading dictionary to memory...')
    parses = _get_word_parses(opencorpora_txt_path)
    logger.debug('dictionary size: %d', len(parses))


    logger.debug('handling umlauts...')
    parses = _add_ee_parses(parses)
    logger.debug('dictionary size: %d', len(parses))

    logger.debug('building test suite...')
    suite = _get_test_suite(parses, word_limit)

    logger.debug('test suite size: %d', len(suite))

    logger.debug('saving...')
    _save_test_suite(out_path, suite)


def to_pymorphy2_format(opencorpora_dict_path, out_path):
    """
    Converts a dictionary from OpenCorpora xml format to
    Pymorphy2 compacted internal format.

    ``out_path`` should be a name of folder where to put dictionaries.
    """
    gramtab, paradigms, words_dawg = _gram_structures(opencorpora_dict_path)
    meta = {'version': 1}

    # create the output folder
    try:
        logger.debug("Creating output folder %s", out_path)
        os.mkdir(out_path)
    except OSError:
        logger.warning("Output folder already exists")

    _f = lambda path: os.path.join(out_path, path)

    logger.info("Saving...")

    with codecs.open(_f('meta.json'), 'w', 'utf8') as f:
        json.dump(meta, f, ensure_ascii=False)

    with codecs.open(_f('gramtab.json'), 'w', 'utf8') as f:
        json.dump(gramtab, f, ensure_ascii=False)

    with codecs.open(_f('paradigms.json'), 'w', 'utf8') as f:
        json.dump(paradigms, f, ensure_ascii=False)

    words_dawg.save(_f('words.dawg'))
