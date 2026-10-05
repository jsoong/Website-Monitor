import pytest

from pagewatch.engine.pipeline.diff import ChangedBlock, diff_blocks
from pagewatch.engine.pipeline.keywords import (
    KeywordSyntaxError,
    colors,
    evaluate,
    parse_number,
    parse_rule,
    parse_rules,
)


def whole(*texts: str) -> list[ChangedBlock]:
    """Blocks that changed entirely (insertions)."""
    return [ChangedBlock(t, [(0, len(t))]) for t in texts]


def fires(rule: str, regions: list[str], page: str | None = None) -> bool:
    return bool(
        evaluate(parse_rules(rule), page_text=page or "\n".join(regions), changes=whole(*regions))
    )


def fires_on_edit(rule: str, old: list[str], new: list[str]) -> bool:
    """Run a rule against the real diff of an edit (old page -> new page)."""
    diff = diff_blocks(old, new)
    return bool(evaluate(parse_rules(rule), page_text="\n".join(new), changes=diff.change_set(new)))


# -- terms ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rule", "regions", "expected"),
    [
        ("watch", ["Try WebSite-Watcher today"], True),  # substring, case-insensitive
        ("WATCH", ["a watcher"], True),
        ('"watch"', ["Try WebSite-Watcher today"], False),  # whole word: 'Watcher' is not 'watch'
        ('"watch"', ["Please watch this"], True),
        ('"watch"', ["a watch."], True),  # punctuation is a boundary
        ('"in stock"', ["Now in stock!"], True),  # whole phrase
        ('"in stock"', ["Now in stocks"], False),
        ("regex(in\\s+stock)", ["now in   stock"], True),
        ("regex(^sale)", ["first line", "sale starts"], True),  # per-line anchors
        ("nothing", ["something else"], False),
        ("a\nb", ["only b here"], True),  # lines are OR-ed
    ],
)
def test_single_terms(rule: str, regions: list[str], expected: bool) -> None:
    assert fires(rule, regions) is expected


def test_and_terms_may_be_anywhere_in_the_changes_by_default() -> None:
    assert fires('"lottery" + "manhattan"', ["New lottery opens", "Apartments in Manhattan"])
    assert not fires('"lottery" + "manhattan"', ["New lottery opens", "Apartments in Brooklyn"])


def test_same_block_scope() -> None:
    rule = '"approved" + "subsidy" [same_block]'
    assert not fires(rule, ["Application approved", "Subsidy letter sent"])
    assert fires(rule, ["Application approved", "Subsidy approved for you"])


def test_near_scope_counts_words_in_the_changes() -> None:
    rule = '"approved" + "subsidy" [near 5]'
    close = ["Your application was approved and the subsidy starts"]
    far = ["approved " + "filler " * 30 + "subsidy"]
    assert fires(rule, close) and not fires(rule, far)
    assert fires('"approved" + "subsidy" [near 40]', far)
    # works across blocks too (the changes are one word sequence)
    assert fires('"approved" + "subsidy" [near 3]', ["approved now", "subsidy next"])


def test_not_terms() -> None:
    assert fires("laptop + -refurbished", ["New laptop in stock"])
    assert not fires("laptop + -refurbished", ["Refurbished laptop in stock"])
    assert not fires(
        "laptop + -refurbished", ["laptop", "refurbished unit"]
    )  # anywhere in the changes


def test_page_context_terms_use_the_whole_page_but_the_rule_still_needs_a_changed_term() -> None:
    page = "RTX 4090 Founders Edition\nPrice $1,099\nIn stock"
    rule = 'page("RTX 4090") + num(\\$([\\d,.]+)) < 1200'
    assert fires(rule, ["Price $1,099"], page)
    assert not fires(rule, ["Price $1,299"], page)  # still above the threshold
    assert not fires(rule, ["Price $1,099"], "Radeon 7900\nPrice $1,099")  # wrong product
    assert not fires(rule, ["unrelated change"], page)  # nothing changed that matches
    assert not fires('-page("sold out") + deal', ["a deal"], "sold out everywhere")
    assert fires('-page("sold out") + deal', ["a deal"], "available")


def test_the_rule_must_have_a_changed_term() -> None:
    for bad in ('page("x")', "-refurbished", 'page("a") + -b'):
        with pytest.raises(KeywordSyntaxError, match="at least one term"):
            parse_rules(bad)


@pytest.mark.parametrize(
    ("rule", "text", "expected"),
    [
        ("num(\\$([\\d,.]+)) < 1200", "now $1,099", True),
        ("num(\\$([\\d,.]+)) < 1200", "now $1,299", False),
        ("num(\\$([\\d,.]+)) <= 1299", "now $1,299", True),
        ("num(\\$([\\d,.]+)) > 1200", "now $1,299.99", True),
        ("num(\\$([\\d,.]+)) = 5", "only $5.00", True),
        ("num(\\$([\\d,.]+)) < 1200", "was $2,000 now $999", True),  # any match satisfies
        ("num((\\d+) items) >= 3", "7 items left", True),
        ("num((\\d+) items) >= 3", "2 items left", False),
        ("num(\\$([\\d,.]+)) < 1200", "no price here", False),
    ],
)
def test_num_comparisons(rule: str, text: str, expected: bool) -> None:
    assert fires(rule, [text]) is expected


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("1,099", 1099.0), ("1,099.50", 1099.5), ("1.099,50", 1099.5), ("12.5", 12.5),
        ("1,5", 1.5), ("1.234.567", 1234567.0), ("$19", 19.0), ("  7 ", 7.0), ("1,234,567", 1234567.0),
        ("abc", None), ("", None), (".,", None),
    ],
)  # fmt: skip
def test_parse_number(text: str, value: float | None) -> None:
    assert parse_number(text) == value


def test_stock_flip_back_needs_no_special_option() -> None:
    # keywords see the changes since the *latest* version, which advanced on the miss
    rule = '"in stock"'
    assert not fires_on_edit(rule, ["Widget", "In stock"], ["Widget", "Out of stock"])  # v1 -> v2
    assert fires_on_edit(rule, ["Widget", "Out of stock"], ["Widget", "In stock"])  # v2 -> v3
    # only "In" changed; the phrase fires because its match overlaps the changed word


def test_a_match_must_overlap_a_changed_span_not_merely_sit_in_a_changed_block() -> None:
    old = ["Big sale on all plans. Updated 5 min ago"]
    new = ["Big sale on all plans. Updated 9 min ago"]
    assert not fires_on_edit("sale", old, new)  # "sale" was already there; only "5"->"9" changed
    assert fires_on_edit("updated", old, new) is False  # same for context words
    assert fires_on_edit("regex(\\d+ min)", old, new)  # a match covering the changed number
    assert fires_on_edit("9", old, new)
    # a brand-new block counts in full
    assert fires_on_edit("sale", ["intro"], ["intro", "sale starts today"])


def test_price_edit_keeps_its_currency_symbol_via_the_overlap_rule() -> None:
    old, new = ["RTX 4090 now $1,299 in stock"], ["RTX 4090 now $1,099 in stock"]
    rule = 'page("RTX 4090") + num(\\$([\\d,.]+)) < 1200'
    assert fires_on_edit(rule, old, new)
    assert not fires_on_edit(rule, new, old)  # going back up to $1,299 does not


def test_same_block_and_near_use_overlapping_matches_only() -> None:
    old = ["Approved today. Subsidy letter mailed Monday"]
    new = ["Approved today. Subsidy letter mailed Tuesday"]
    assert not fires_on_edit('"approved" + "subsidy" [same_block]', old, new)  # neither changed
    assert fires_on_edit('"tuesday" + "subsidy"', old, new) is False  # subsidy did not change
    assert fires_on_edit('"tuesday" + "mailed" [near 3]', ["x"], ["Subsidy letter mailed Tuesday"])


def test_not_terms_only_count_when_they_overlap_the_change() -> None:
    old = ["Refurbished laptop 15 inch"]
    new = ["Refurbished laptop 17 inch"]
    # 'refurbished' sits in unchanged text of the changed block, so -refurbished does not veto;
    # but the rule also needs a changed positive term:
    assert fires_on_edit("17 + -refurbished", old, new)  # 17 changed; refurbished did not change
    assert not fires_on_edit("17 + -refurbished", ["x"], ["Refurbished laptop 17 inch"])


# -- parsing ----------------------------------------------------------------------------


def test_plus_inside_quotes_and_regex_does_not_split() -> None:
    r = parse_rule('"c++" + regex(a+b)')
    assert [t.kind for t in r.terms] == ["word", "regex"]
    assert fires('"c++" + regex(x+y)', ["learn c++ and xxy"])


def test_colour_and_scope_suffixes_in_either_order() -> None:
    a = parse_rule('"x" + "y" [near 10] #red')
    b = parse_rule('"x" + "y" #red [near 10]')
    assert (a.scope, a.near, a.color) == ("near", 10, "red") == (b.scope, b.near, b.color)
    assert a.source == '"x" + "y"'
    c = parse_rule("sale #ff0000")
    assert c.color == "ff0000" and c.source == "sale"
    assert colors(parse_rules("sale #red\nbargain\nclearance #blue")) == {
        "sale": "red",
        "clearance": "blue",
    }


def test_comments_blank_lines_and_hit_sources() -> None:
    rules = parse_rules('// watch for these\n\nsale #red\n"new" + "arrival"')
    assert [r.source for r in rules] == ["sale", '"new" + "arrival"']
    assert evaluate(rules, page_text="x", changes=whole("a big sale")) == ["sale"]
    assert evaluate(rules, page_text="x", changes=whole("a new arrival")) == ['"new" + "arrival"']
    both = evaluate(rules, page_text="x", changes=whole("big sale and a new arrival"))
    assert both == ["sale", '"new" + "arrival"']  # every rule that fires is reported


@pytest.mark.parametrize(
    ("bad", "needle"),
    [
        ('"unterminated', "quote"),
        ("regex(", "parenthes"),
        ("regex()", "empty"),
        ("regex([)", "regular expression"),
        ("num(\\d+) <", "comparison"),
        ("num(\\d+) < 5", "capture group"),
        ("num((\\d+)) < x", "comparison"),
        ("a + + b", "empty term"),
        ("a +", "empty term"),
        ("solo [same_block]", "at least two"),
        ("page(-x) + y", "-page"),
        ('""', "empty"),
    ],
)
def test_syntax_errors_carry_a_line_number_and_message(bad: str, needle: str) -> None:
    with pytest.raises(KeywordSyntaxError) as exc:
        parse_rules("fine\n" + bad)
    assert exc.value.line == 2 and needle in str(exc.value)


def test_nested_parentheses_in_regex_and_page() -> None:
    r = parse_rule(r"page(regex(a(b|c)d)) + hello")
    assert r.terms[0].page and r.terms[0].kind == "regex"
    assert fires(r"page(regex(a(b|c)d)) + hello", ["hello"], "xx acd yy")
    assert not fires(r"page(regex(a(b|c)d)) + hello", ["hello"], "xx aed yy")
