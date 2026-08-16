"""分词与词元归一（DESIGN §6 ingest 行）。"""

from __future__ import annotations

from app.ingest import lemmatize, normalize_surface, tokenize


def surfaces(text):
    return [t.surface for t in tokenize(text)]


def test_tokens_are_lowercased():
    toks = tokenize("The Machine Sees Everything.")
    assert [t.surface for t in toks] == ["the", "machine", "sees", "everything"]
    assert all(t.surface == t.surface.lower() for t in toks)
    assert all(t.lemma == t.lemma.lower() for t in toks)


def test_contractions_stay_whole():
    assert surfaces("It's fine, don't worry.") == ["it's", "fine", "don't", "worry"]
    assert surfaces("We're sure they've gone.") == ["we're", "sure", "they've", "gone"]


def test_curly_apostrophe_normalized():
    assert surfaces("It’s late.") == ["it's", "late"]
    assert normalize_surface("DON’T") == "don't"


def test_punctuation_stripped_but_hyphen_kept():
    assert surfaces('"Stop!" -- she said (loudly).') == ["stop", "she", "said", "loudly"]
    assert surfaces("An ex-con walked in.") == ["an", "ex-con", "walked", "in"]
    # 对话破折号不粘进 token
    assert surfaces("- Carla. - Hey.") == ["carla", "hey"]


def test_digits_are_not_tokens():
    assert surfaces("3 days ago, 24/7 duty.") == ["days", "ago", "duty"]


def test_repeated_word_yields_separate_tokens():
    toks = tokenize("The cameras were cheap; the cameras broke.")
    cams = [t for t in toks if t.surface == "cameras"]
    assert len(cams) == 2
    assert cams[0].char_start != cams[1].char_start
    the = [t for t in toks if t.surface == "the"]
    assert len(the) == 2


def test_char_offsets_point_back_at_source_text():
    text = "Marlow says it's raining again."
    for tok in tokenize(text):
        assert text[tok.char_start : tok.char_end].lower() == tok.surface


def test_char_offsets_span_multiline_text():
    text = "first line here\nsecond line there"
    toks = tokenize(text)
    assert [text[t.char_start : t.char_end] for t in toks] == [
        "first",
        "line",
        "here",
        "second",
        "line",
        "there",
    ]


def test_proper_nouns_are_not_excluded():
    toks = tokenize("Marlow met Bramwell on Halloway Street.")
    got = {t.surface for t in toks}
    assert {"marlow", "bramwell", "halloway", "street"} <= got


def test_lemma_normalization_common_cases():
    cases = {
        "went": "go",
        "cousins": "cousin",
        "cameras": "camera",
        "sees": "see",
        "bought": "buy",
        "children": "child",
        "men": "man",
        "raining": "rain",
        "stopped": "stop",
        "is": "be",
        "was": "be",
        "better": "good",
        "shouted": "shout",
    }
    for surface, lemma in cases.items():
        assert lemmatize(surface) == lemma, f"{surface} -> {lemmatize(surface)}"


def test_lemma_is_always_lowercase():
    # simplemma 对 'i' 会吐大写 'I'，必须被压回小写
    assert lemmatize("i") == "i"
    assert lemmatize("us") == "we"


def test_lemma_of_unknown_proper_noun_falls_back_to_surface():
    assert lemmatize("bramwell") == "bramwell"
    assert lemmatize("halloway") == "halloway"


def test_possessive_is_stripped_by_lemma():
    toks = tokenize("My cousin's cop friend called.")
    by_surface = {t.surface: t.lemma for t in toks}
    assert by_surface["cousin's"] == "cousin"


def test_tokenize_empty_and_punct_only():
    assert tokenize("") == []
    assert tokenize("!!! ... ---") == []


def test_contraction_lemmas_use_deterministic_rules():
    """simplemma 在缩合形上不可靠（it's→its、i'm→i'm），前置规则抃住。"""
    cases = {
        "it's": "it",
        "i'm": "i",
        "you're": "you",
        "they've": "they",
        "he's": "he",
        "she'd": "she",
        "let's": "let",
        "don't": "do",
        "didn't": "do",
        "shouldn't": "should",
        "can't": "can",
        "won't": "will",
        "isn't": "be",
        "cousin's": "cousin",
    }
    for surface, lemma in cases.items():
        assert lemmatize(surface) == lemma, f"{surface} -> {lemmatize(surface)}"


def test_modals_are_their_own_lemma():
    for w in ("should", "would", "could", "might", "must", "can", "will"):
        assert lemmatize(w) == w


def test_non_clitic_apostrophe_word_untouched():
    # o'clock 的 'clock 不是 clitic，不能被劈成 o
    assert lemmatize("o'clock") == "o'clock"
