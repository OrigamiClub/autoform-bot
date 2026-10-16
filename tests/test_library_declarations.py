import json

import pytest

from autoform_cli.library.declarations import (
    ProbeConstant,
    ProbeOutputError,
    classify,
    parse_probe_output,
    starts_with_module,
)

STANDARD = ("Classical.choice", "Quot.sound", "propext")


def constant(source: str, name: str, written: str, *, start: str | None = None, **changes) -> ProbeConstant:
    """A probe record for the declaration of ``source`` whose name is written as ``written``.

    The range starts at ``start`` (default: the start of the file) and ends at the end of the file.
    """

    lines = source.split("\n")
    offset = source.index(start) if start is not None else 0
    line = source.count("\n", 0, offset)
    column = offset - (source.rfind("\n", 0, offset) + 1)
    at = source.index(written, offset)
    selection_line = source.count("\n", 0, at)
    selection_column = at - (source.rfind("\n", 0, at) + 1)
    values = {
        "name": name,
        "module": "Lib.A",
        "private": False,
        "wrapper": None,
        "range": ((line + 1, column), (len(lines), len(lines[-1]))),
        "selection": ((selection_line + 1, selection_column), (selection_line + 1, selection_column + len(written))),
        "signature": "True",
        "docstring": None,
        "mentions": ("True",),
        "axioms": (),
    }
    values.update(changes)
    return ProbeConstant(**values)


def test_a_theorem_is_indexed_with_its_keyword_line_and_header() -> None:
    source = "/-- Doubling. -/\n@[simp] theorem double_eq (n : Nat) :\n    n + n = 2 * n := by omega\n"
    record = classify(constant(source, "Lib.double_eq", "double_eq", docstring="Doubling."), source)
    assert (record.kind, record.status, record.line) == ("theorem", "complete", 2)
    assert record.statement == "theorem double_eq (n : Nat) :\n    n + n = 2 * n"
    assert record.docstring == "Doubling."
    assert record.auto_named is False


@pytest.mark.parametrize(
    ("source", "line"),
    [
        ("theorem claim : True := trivial\n", 1),
        ("/-- One line. -/\ntheorem claim : True := trivial\n", 2),
        ("/-- Several\nlines\n  of docstring.\n-/\ntheorem claim : True := trivial\n", 5),
        ("@[simp]\ntheorem claim : True := trivial\n", 2),
        ("@[simp,\n  norm_cast]\ntheorem claim : True := trivial\n", 3),
        ("/-- Doc. -/\n@[simp]\n-- a note\ntheorem claim : True := trivial\n", 4),
        # The first modifier, not the keyword after it.
        ("/-- Doc. -/\n@[simp]\nprotected\n  theorem claim : True := trivial\n", 3),
        ("/-- Doc. -/ @[simp] theorem claim : True := trivial\n", 1),
    ],
)
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_the_line_is_the_line_of_the_first_modifier_or_keyword(source: str, line: int, newline: str) -> None:
    record = classify(constant(source, "Lib.claim", "claim"), source.replace("\n", newline))
    assert record.line == line
    assert record.statement.startswith(("theorem claim", "protected\n  theorem claim"))


def test_the_line_of_a_wanted_result_is_the_line_of_its_command() -> None:
    source = "/-- Wanted,\nfor later. -/\npublic theorem_wanted claim : True\n"
    record = classify(constant(source, "Lib.claim", "claim", private=True, wrapper="ProofWanted"), source)
    assert (record.status, record.line) == ("wanted", 3)


def test_the_line_counts_from_where_the_range_starts() -> None:
    source = "def before : Nat := 0\n\n/-- Doc. -/\n@[simp]\ntheorem claim : True := trivial\n"
    assert classify(constant(source, "Lib.claim", "claim", start="/-- Doc."), source).line == 5


@pytest.mark.parametrize(
    "keyword", ["theorem", "lemma", "def", "abbrev", "structure", "class", "inductive", "instance", "opaque"]
)
def test_the_kind_is_the_keyword_as_written(keyword: str) -> None:
    source = f"public noncomputable {keyword} thing : Nat := 0\n"
    record = classify(constant(source, "Lib.thing", "thing"), source)
    assert record.kind == keyword
    assert record.statement == f"public noncomputable {keyword} thing : Nat"


def test_a_name_written_with_a_namespace_prefix_is_indexed() -> None:
    # `theorem Valid.sublist` inside `namespace Forest`: the selection is the two written components.
    source = "theorem Valid.sublist : True := trivial\n"
    record = classify(constant(source, "Lib.Forest.Valid.sublist", "Valid.sublist"), source)
    assert record.name == "Lib.Forest.Valid.sublist"


@pytest.mark.parametrize(
    ("written", "name"),
    [
        # Lean prints a component that is not an identifier in quotes, and one that is without them.
        ("«odd name»", "Lib.«odd name»"),
        ("«plain»", "Lib.plain"),
        ("«odd name».inner", "Lib.«odd name».inner"),
        ("Outer.«odd name»", "Lib.Outer.«odd name»"),
    ],
)
def test_a_quoted_name_component_matches_the_name(written: str, name: str) -> None:
    source = f"theorem {written} : True := trivial\n"
    assert classify(constant(source, name, written), source).name == name


@pytest.mark.parametrize(
    ("written", "name"),
    [("_root_.Nat.foo", "Nat.foo"), ("_root_.foo", "foo"), ("_root_.«odd name».foo", "«odd name».foo")],
)
def test_a_name_written_from_the_root_namespace_is_indexed_under_its_name(written: str, name: str) -> None:
    # `theorem _root_.Nat.foo` inside `namespace Lib` declares `Nat.foo`.
    source = f"theorem {written} : True := trivial\n"
    record = classify(constant(source, name, written), source)
    assert (record.name, record.statement) == (name, f"theorem {written} : True")


@pytest.mark.parametrize(
    ("written", "name"), [("_root_.Nat.foo", "Nat.bar"), ("_root_._root_.foo", "foo"), ("x_root_.foo", "foo")]
)
def test_only_one_leading_root_prefix_of_the_written_name_is_dropped(written: str, name: str) -> None:
    source = f"theorem {written} : True := trivial\n"
    assert classify(constant(source, name, written), source) == "name"


@pytest.mark.parametrize(
    "changes",
    [
        # The source is shorter than the probe says: it is not the text Lean compiled.
        {"range": ((1, 0), (3, 0))},
        {"range": ((1, 0), (1, 200))},
        {"range": ((1, 200), (2, 0))},
        {"selection": ((7, 8), (7, 13))},
        {"selection": ((1, 8), (1, 200))},
        {"selection": ((1, 200), (1, 201))},
        {"selection": ((1, 8), (9, 0))},
    ],
)
def test_a_range_outside_the_source_is_refused_with_the_declaration_and_module(changes: dict) -> None:
    source = "theorem claim : True := trivial\n"
    with pytest.raises(ProbeOutputError, match=r"Lib\.claim in Lib\.A .* the source"):
        classify(constant(source, "Lib.claim", "claim", **changes), source)


def test_a_range_that_ends_at_the_end_of_the_source_is_read() -> None:
    source = "theorem claim : True := trivial\n"
    record = classify(constant(source, "Lib.claim", "claim", range=((1, 0), (2, 0))), source)
    assert record.statement == "theorem claim : True"


def test_a_selection_that_is_not_the_name_is_left_out() -> None:
    source = "theorem other : True := trivial\n"
    assert classify(constant(source, "Lib.parser_1", "other"), source) == "name"


@pytest.mark.parametrize(
    ("source", "start", "written"),
    [
        # A derived instance: the range is the class name after `deriving`.
        ("structure P where\n  x : Nat\nderiving DecidableEq\n", "DecidableEq", "DecidableEq"),
        # A theorem `@[ext]` adds to a structure: the range is the attribute's own token.
        ("@[ext] structure P where\n  x : Nat\n", "ext]", "ext"),
        # The additive copy of a `@[to_additive]` declaration.
        ("@[to_additive]\ntheorem mul_thing : True := trivial\n", "to_additive", "to_additive"),
        # A `where` helper: the range starts at the helper's own name.
        ("def sum : Nat := go\nwhere\n  go : Nat := 0\n", "go : Nat", "go"),
    ],
)
def test_what_a_command_generates_beside_its_declaration_is_left_out(source: str, start: str, written: str) -> None:
    assert classify(constant(source, "Lib.x." + written, written, start=start), source) == "generated"


def test_a_hand_written_ext_theorem_is_indexed() -> None:
    source = "@[ext] theorem ext (a b : Nat) : a = b → a = b := id\n"
    assert classify(constant(source, "Lib.P.ext", "ext", start="@[ext]"), source).kind == "theorem"


def test_an_anonymous_instance_is_indexed_as_auto_named() -> None:
    source = "instance : Inhabited Nat := ⟨0⟩\n"
    record = classify(constant(source, "Lib.instInhabitedNat", "instance"), source)
    assert (record.kind, record.auto_named) == ("instance", True)


@pytest.mark.parametrize("axioms", [("sorryAx",), ("Lib.assumed", "propext")])
def test_a_declaration_that_rests_on_another_axiom_is_left_out(axioms: tuple[str, ...]) -> None:
    source = "theorem gap : True := trivial\n"
    assert classify(constant(source, "Lib.gap", "gap", axioms=axioms), source) == "axioms"


def test_the_three_standard_axioms_are_complete() -> None:
    source = "theorem fine : True := trivial\n"
    assert classify(constant(source, "Lib.fine", "fine", axioms=STANDARD), source).status == "complete"


def test_an_axiom_declaration_is_left_out_and_counted() -> None:
    source = "axiom assumed : True\n"
    assert classify(constant(source, "Lib.assumed", "assumed"), source) == "axioms"


@pytest.mark.parametrize(
    ("command", "kind"),
    [("theorem_wanted", "theorem"), ("proof_wanted", "theorem"), ("def_wanted", "def"), ("instance_wanted", "instance")],
)
def test_a_wanted_command_is_indexed_as_wanted(command: str, kind: str) -> None:
    source = f"/-- An established result. -/\npublic {command} claim (n : Nat) : n = n\n"
    record = classify(
        constant(source, "Lib.claim", "claim", private=True, wrapper="ProofWanted"), source
    )
    assert (record.kind, record.status) == (kind, "wanted")
    # The environment holds no docstring for a wanted placeholder; it comes from the source.
    assert record.docstring == "An established result."
    assert record.statement == f"public {command} claim (n : Nat) : n = n"


@pytest.mark.parametrize("source", ["instance_wanted : Nonempty Empty\n", "/-- Wanted. -/\ninstance_wanted : Nonempty Empty\n"])
def test_an_anonymous_wanted_instance_is_indexed_as_auto_named(source: str) -> None:
    # Batteries selects the whole command for the name it makes, docstring included.
    whole = constant(source.rstrip("\n"), "Lib.instNonempty", "instance_wanted", private=True, wrapper="DefWanted").range
    record = classify(
        constant(source, "Lib.instNonempty", "instance_wanted", private=True, wrapper="DefWanted", range=whole, selection=whole),
        source,
    )
    assert (record.kind, record.status, record.auto_named) == ("instance", "wanted", True)
    assert record.statement == "instance_wanted : Nonempty Empty"


def test_a_selection_of_the_whole_command_is_a_name_only_for_a_wanted_instance() -> None:
    source = "theorem_wanted claim : True\n"
    whole = ((1, 0), (1, len(source) - 1))
    record = constant(source, "Lib.other", "claim", private=True, wrapper="ProofWanted", range=whole, selection=whole)
    assert classify(record, source) == "name"


def test_a_wanted_docstring_written_above_the_range_is_found() -> None:
    source = "def before : Nat := 0\n\n/-- Above. -/\ntheorem_wanted claim : True\n"
    record = classify(
        constant(source, "Lib.claim", "claim", start="theorem_wanted", private=True, wrapper="ProofWanted"), source
    )
    assert record.docstring == "Above."


def test_a_hand_written_placeholder_is_left_out() -> None:
    source = "private def claim : ProofWanted True := ⟨⟩\n"
    assert classify(constant(source, "Lib.claim", "claim", private=True, wrapper="ProofWanted"), source) == "wrapper"


@pytest.mark.parametrize(
    ("source", "statement"),
    [
        ("structure P (α : Type) where\n  x : α\n", "structure P (α : Type)"),
        ("inductive C\n  | red\n  | green\n", "inductive C"),
        ("def f (n : Nat := 3) : Nat := n\n", "def f (n : Nat := 3) : Nat"),
        ('def s : String := "a := b"\n', "def s : String"),
        ("def g : Nat -- where := later\n  := 0\n", "def g : Nat -- where := later"),
        ("def h : Nat\n", "def h : Nat"),
    ],
)
def test_the_statement_stops_at_the_body(source: str, statement: str) -> None:
    name = statement.split()[1]
    assert classify(constant(source, f"Lib.{name}", name), source).statement == statement


@pytest.mark.parametrize(
    ("source", "statement"),
    [
        # After the colon no type has been written yet, so the bar opens one: an absolute value.
        ("theorem t (x : Int) :\n    |x| ≤ |x| := le_rfl\n", "theorem t (x : Int) :\n    |x| ≤ |x|"),
        ("theorem t (x : Int) : -- the bound\n    |x| ≤ |x| := le_rfl\n", "theorem t (x : Int) : -- the bound\n    |x| ≤ |x|"),
        ("theorem t (x : Int) : /- the\nbound -/\n|x| ≤ |x| := le_rfl\n", "theorem t (x : Int) : /- the\nbound -/\n|x| ≤ |x|"),
        # The same after a sign that still needs its right operand.
        ("theorem t (x : Int) : 0 ≤ 1 ∧\n    |x| ≤ |x| := sorry\n", "theorem t (x : Int) : 0 ≤ 1 ∧\n    |x| ≤ |x|"),
        ("theorem t : ∀ x : Int, 0 ≤ x →\n    |x| = x := sorry\n", "theorem t : ∀ x : Int, 0 ≤ x →\n    |x| = x"),
        ("theorem t : ∀ x : Int,\n    |x| = |x| := sorry\n", "theorem t : ∀ x : Int,\n    |x| = |x|"),
        ("theorem t (x : Int) : x * x =\n    |x| * |x| := sorry\n", "theorem t (x : Int) : x * x =\n    |x| * |x|"),
        ("theorem t (x : Int) : 0 ≤ 2 *\n    |x| ≤\n    |x| := sorry\n", "theorem t (x : Int) : 0 ≤ 2 *\n    |x| ≤\n    |x|"),
        ("theorem t (x : Int) : 0 ≤ x ->\n    |x| = x := sorry\n", "theorem t (x : Int) : 0 ≤ x ->\n    |x| = x"),
        # An absolute value may close on a later line.
        (
            "theorem t (x : Int) : 0 ≤ x →\n    |x -\n      x ^ 2| ≤ 1 := sorry\n",
            "theorem t (x : Int) : 0 ≤ x →\n    |x -\n      x ^ 2| ≤ 1",
        ),
    ],
)
def test_a_bar_that_starts_a_line_where_a_type_cannot_end_is_part_of_the_statement(source: str, statement: str) -> None:
    assert classify(constant(source, "Lib.t", "t"), source).statement == statement


@pytest.mark.parametrize(
    ("source", "statement"),
    [
        # A wanted command has no alternatives and no `where`: without `:=` its statement is all of it.
        (
            "theorem_wanted t (n : Nat) :\n    match n with\n    | 0 => True\n    | _ => n = n\n",
            "theorem_wanted t (n : Nat) :\n    match n with\n    | 0 => True\n    | _ => n = n",
        ),
        ("def_wanted t : Nat → ℕ+\n", "def_wanted t : Nat → ℕ+"),
        ("def_wanted t :\n    let where_ := 1; Fin (where_ + 1)\n", "def_wanted t :\n    let where_ := 1; Fin (where_ + 1)"),
        # With a `:=` it has a body, which is no part of the statement.
        ("def_wanted t : Nat := ❰other❱ + 1\n", "def_wanted t : Nat"),
        ("theorem_wanted t : let n := 2; n = 2 :=\n  ❰other❱\n", "theorem_wanted t : let n := 2; n = 2"),
        ("instance_wanted t : Inhabited Nat := ⟨❰other❱⟩\n", "instance_wanted t : Inhabited Nat"),
    ],
)
def test_the_statement_of_a_wanted_command_ends_only_at_its_body(source: str, statement: str) -> None:
    record = classify(constant(source.rstrip("\n"), "Lib.t", "t", private=True, wrapper="ProofWanted"), source)
    assert record.statement == statement


def test_a_wanted_statement_that_goes_on_with_an_absolute_value_is_whole() -> None:
    source = "theorem_wanted t (x : Int) :\n    |x| ≥ 0\n"
    record = classify(constant(source.rstrip("\n"), "Lib.t", "t", private=True, wrapper="ProofWanted"), source)
    assert record.statement == "theorem_wanted t (x : Int) :\n    |x| ≥ 0"


@pytest.mark.parametrize(
    ("source", "statement"),
    [
        ("theorem t : ∀ n : Nat, n = n\n  | 0 => rfl\n  | _ + 1 => rfl\n", "theorem t : ∀ n : Nat, n = n"),
        ("def t : Nat → Nat\n  | 0 => 1\n  | n + 1 => n\n", "def t : Nat → Nat"),
        ("def t (b : Bool) : Nat → Bool\n|0 => b\n|_ => !b\n", "def t (b : Bool) : Nat → Bool"),
        ("theorem t : (n : Nat) → n ≤ n * n ∧ True\n  | 0 => by simp\n  | _ + 1 => sorry\n", "theorem t : (n : Nat) → n ≤ n * n ∧ True"),
        # `Type*` and `Sort*` end a type; the star is no multiplication.
        ("def t : Nat → Type*\n  | _ => Nat\n", "def t : Nat → Type*"),
        ("def t : Nat → Sort*\n  | _ => Nat\n", "def t : Nat → Sort*"),
        # A sign inside a comment is not the end of the header.
        ("def t : Nat → Nat -- maps →\n  | n => n\n", "def t : Nat → Nat -- maps →"),
        ("instance t : Inhabited Nat\n  | none => 0\n", "instance t : Inhabited Nat"),
        ("inductive t : Type\n  | one : t\n  | two : t\n", "inductive t : Type"),
        # A type may end in a sign: `ℕ+` is the positive naturals, `ℝ*` the hyperreals. The bar's own
        # line then shows it is a match alternative: a `=>` comes before any `:=`.
        ("def t : ℕ → ℕ+\n  | 0 => 1\n  | n + 1 => t n\n", "def t : ℕ → ℕ+"),
        ("def t : ℕ → ℝ*\n  | _ => 0\n", "def t : ℕ → ℝ*"),
        ("def t : ℕ → ℕ+\n|0 => 1\n|_ => 2\n", "def t : ℕ → ℕ+"),
        ("def t : ℕ → ℕ+\n  |0 | 1 => 1\n  |_ => 2\n", "def t : ℕ → ℕ+"),
        (
            "def t : ℕ → List ℕ+\n  | n =>\n    if h : 0 < n then\n      have : n / 2 < n := Nat.div_lt_self h (by decide)\n"
            "      [] else []\n",
            "def t : ℕ → List ℕ+",
        ),
        ("def t : ℕ → ℕ+\n  |n =>\n    have h : 0 < n + 1 := by omega\n    ⟨n + 1, h⟩\n", "def t : ℕ → ℕ+"),
        # An absolute value has a closing bar; a bar with none that is not at the start of a line is not evidence of one.
        ("inductive t : ℕ → ℕ+\n  |one : t\n", "inductive t : ℕ → ℕ+"),
        ("def t : ℕ → ℝ*\n  |n\n", "def t : ℕ → ℝ*"),
        ("inductive t : ℕ → ℕ+\n  |one : t\n  |two : t\n", "inductive t : ℕ → ℕ+"),
        ("def t : ℕ → Type*\n  |n => ℕ\n", "def t : ℕ → Type*"),
        (
            "def sliceSizes : ℕ → MLList Id ℕ+\n  | n =>\n    if h : 0 < n then\n      have : n / 2 < n := Nat.div_lt_self h (by decide)\n"
            "      pure n else pure 1\n",
            "def sliceSizes : ℕ → MLList Id ℕ+",
        ),
        # An absolute value is written with nothing after its first bar.
        ("inductive t : ℕ → ℕ+\n  | one : t\n", "inductive t : ℕ → ℕ+"),
        # In doubt the bar ends the statement: here a `=>` of the type itself follows it.
        (
            "theorem t (f : Int → Int) : f = id ∧\n    |f 0| = 0 ∧ f = fun z => z := sorry\n",
            "theorem t (f : Int → Int) : f = id ∧",
        ),
    ],
)
def test_a_bar_that_starts_a_line_after_a_whole_type_ends_the_statement(source: str, statement: str) -> None:
    assert classify(constant(source, "Lib.t", "t"), source).statement == statement


@pytest.mark.parametrize(
    ("source", "statement"),
    [
        ("theorem t :\n    let n := 2\n    n = 2 := rfl\n", "theorem t :\n    let n := 2\n    n = 2"),
        ("theorem t : let n : Nat := 2; let m := n; m = 2 := rfl\n", "theorem t : let n : Nat := 2; let m := n; m = 2"),
        ("theorem t : ∃ p : Nat, letI : Inhabited Nat := ⟨p⟩; True := ⟨0, trivial⟩\n", "theorem t : ∃ p : Nat, letI : Inhabited Nat := ⟨p⟩; True"),
        ("theorem t : have h : True := trivial; haveI := h; True := trivial\n", "theorem t : have h : True := trivial; haveI := h; True"),
        ("theorem t (x : Int) :\n    let y :=\n      |x|\n    y = y := rfl\n", "theorem t (x : Int) :\n    let y :=\n      |x|\n    y = y"),
        # A `let` inside brackets is closed with them, and one in the body is the body's.
        ("def t (n : Nat := let k := 1; k) : Nat := n\n", "def t (n : Nat := let k := 1; k) : Nat"),
        ("def t : Nat :=\n  let k := 1\n  k\n", "def t : Nat"),
        ("def t (letter : Nat) : Nat := letter\n", "def t (letter : Nat) : Nat"),
        ("def t : Nat.let := 0\n", "def t : Nat.let"),
        # A `let` defined by alternatives has no `:=`: the next one is the declaration's.
        (
            "theorem t : let f : Nat → Nat | 0 => 1 | _ => 2; f 0 = 1 := by\n  intro f\n  have h := rfl\n  exact h\n",
            "theorem t : let f : Nat → Nat | 0 => 1 | _ => 2; f 0 = 1",
        ),
        ("theorem t : have h : True | _ => trivial; True := by\n  have k := 1\n  trivial\n", "theorem t : have h : True | _ => trivial; True"),
        # Not the words `let` and `have`: other names that hold them.
        ("theorem t : let' = 1 := by\n  simp\n  obtain x := 1\n", "theorem t : let' = 1"),
        ("theorem t : have? = 1 := by\n  simp\n  obtain x := 1\n", "theorem t : have? = 1"),
        ("theorem t : let! = 1 := by\n  simp\n  obtain x := 1\n", "theorem t : let! = 1"),
        ("theorem t : «let» = 1 := by\n  simp\n  obtain x := 1\n", "theorem t : «let» = 1"),
        ("theorem t : letα = 1 := by\n  simp\n  obtain x := 1\n", "theorem t : letα = 1"),
        ("theorem t : h'let = 1 := by\n  simp\n  obtain x := 1\n", "theorem t : h'let = 1"),
        ("theorem t : «where» = 1 := by\n  simp\n", "theorem t : «where» = 1"),
        ("def t : where' := 0\n", "def t : where'"),
    ],
)
def test_the_definition_sign_of_a_let_in_the_type_does_not_end_the_statement(source: str, statement: str) -> None:
    assert classify(constant(source, "Lib.t", "t"), source).statement == statement


@pytest.mark.parametrize(
    ("source", "statement"),
    [
        # Alternatives written on the line of the type.
        ("theorem foo : ∀ n : Nat, n = n | 0 => rfl | n+1 => rfl\n", "theorem foo : ∀ n : Nat, n = n"),
        ("def foo : Nat → Nat | 0 => 1 | n+1 => 2\n", "def foo : Nat → Nat"),
        # Shaped like Mathlib's `cast_nonneg`, `unbot` and `coe_unbot`.
        (
            "@[simp] lemma foo : ∀ {n : ℤ}, 0 ≤ n → (0 : R) ≤ n | (n : ℕ), _ => by simp\n",
            "lemma foo : ∀ {n : ℤ}, 0 ≤ n → (0 : R) ≤ n",
        ),
        ("def foo : ∀ x : WithBot α, x ≠ ⊥ → α | (x : α), _ => x\n", "def foo : ∀ x : WithBot α, x ≠ ⊥ → α"),
        ("lemma foo : ∀ (x : WithBot α) hx, x.unbot hx = x | (x : α), _ => rfl\n", "lemma foo : ∀ (x : WithBot α) hx, x.unbot hx = x"),
        # A bar after the first one does not matter: the first ends the statement.
        ("theorem foo : ∀ b : Bool, b = b | true => rfl | false => rfl\n", "theorem foo : ∀ b : Bool, b = b"),
        # An absolute value in the middle of a line has no `=>` after it before the body.
        ("theorem foo (x : Int) : 0 ≤ |x| := abs_nonneg x\n", "theorem foo (x : Int) : 0 ≤ |x|"),
        ("theorem foo (x y : Int) : |x| ≤ |x - y| + |y| := by\n  intro h\n  exact fun a => a\n", "theorem foo (x y : Int) : |x| ≤ |x - y| + |y|"),
        # An operator that holds a bar is no bar; an absolute value stays whole when a `=>` follows it.
        ("protected def foo (v : K) : Stream' <| Option (Pair K) := ⟨⟩\n", "protected def foo (v : K) : Stream' <| Option (Pair K)"),
        ("theorem foo (f : Lp E p μ) : ⇑|f| =ᵐ[μ] fun x => |f x| := coeFn_abs f\n", "theorem foo (f : Lp E p μ) : ⇑|f| =ᵐ[μ] fun x => |f x|"),
        ("theorem foo (a : ℤ) : f a |>.1 = |a| := rfl\n", "theorem foo (a : ℤ) : f a |>.1 = |a|"),
        # A `=>` inside brackets is not an alternative's.
        ("theorem foo (f : Nat → Nat) : f = (fun x => x) ∧ |(0 : Int)| = 0 := rfl\n", "theorem foo (f : Nat → Nat) : f = (fun x => x) ∧ |(0 : Int)| = 0"),
    ],
)
def test_a_bar_in_the_middle_of_a_line_ends_the_statement_when_alternatives_follow(source: str, statement: str) -> None:
    assert classify(constant(source, "Lib.foo", "foo"), source).statement == statement


@pytest.mark.parametrize(
    ("source", "statement"),
    [
        ("inductive Foo : Type | a | b\n  deriving Repr\n", "inductive Foo : Type"),
        ("inductive ExceptType | tooSmall | notComparable\n", "inductive ExceptType"),
        ("inductive Two where | zero | one\n", "inductive Two"),
        ("inductive Foo : Type\n  | a\n  | b\n", "inductive Foo : Type"),
        ("class inductive Foo : Prop | intro : Foo\n", "class inductive Foo : Prop"),
        ("inductive Foo (α : Type) : Type | mk (a : α) | nil\n", "inductive Foo (α : Type) : Type"),
        ("inductive Foo (n : Fin |x|) | mk\n", "inductive Foo (n : Fin |x|)"),
    ],
)
def test_the_statement_of_an_inductive_type_ends_before_its_first_constructor(source: str, statement: str) -> None:
    name = next(word for word in statement.split() if word not in ("class", "inductive"))
    assert classify(constant(source, f"Lib.{name}", name), source).statement == statement


@pytest.mark.parametrize(
    ("source", "statement"),
    [
        ("theorem foo (c : Char) (h : c = '\"') : P c := by simp\n", "theorem foo (c : Char) (h : c = '\"') : P c"),
        ("theorem foo (c : Char) (h : c = '\\'') : P c := by simp\n", "theorem foo (c : Char) (h : c = '\\'') : P c"),
        ("theorem foo (c : Char) (h : c = '(') : P c := by simp\n", "theorem foo (c : Char) (h : c = '(') : P c"),
        ("theorem foo (c : Char) (h : c = '\\n') : P c := by simp\n", "theorem foo (c : Char) (h : c = '\\n') : P c"),
        ("theorem foo (c : Char) (h : c = '\\x41') : P c := by simp\n", "theorem foo (c : Char) (h : c = '\\x41') : P c"),
        ("theorem foo (c : Char) (h : c = '\\u{1F600}') : P c := by simp\n", "theorem foo (c : Char) (h : c = '\\u{1F600}') : P c"),
        ("theorem foo : c = '\"' ∧ d = '\"' := by simp\n", "theorem foo : c = '\"' ∧ d = '\"'"),
        # A prime is part of an identifier, and a quote after it starts a string.
        ("theorem foo (x' : Nat) (h : x' = 1) : s = \"a := b\" := by simp\n", "theorem foo (x' : Nat) (h : x' = 1) : s = \"a := b\""),
        ("theorem foo (x'' y' : Nat) : x'' = y' := by simp\n", "theorem foo (x'' y' : Nat) : x'' = y'"),
        ("theorem foo (x' : Nat) : s = \"it's\" := by simp\n", "theorem foo (x' : Nat) : s = \"it's\""),
        # Raw strings.
        ('theorem foo : s = r"a\\" := by simp\n', 'theorem foo : s = r"a\\"'),
        ('theorem foo : s = r"a := b" := by simp\n', 'theorem foo : s = r"a := b"'),
        ('theorem foo : s = r#"a" := b"# := by simp\n', 'theorem foo : s = r#"a" := b"#'),
        ('theorem foo : s = r##"a"# := b"## := by simp\n', 'theorem foo : s = r##"a"# := b"##'),
        # A name that ends in `r` followed by a string is not a raw string.
        ('theorem foo (r : String) : r = "a" := by simp\n', 'theorem foo (r : String) : r = "a"'),
        ('theorem foo (bar : String) : bar ++ "x := y" = "" := by simp\n', 'theorem foo (bar : String) : bar ++ "x := y" = ""'),
    ],
)
def test_char_literals_and_raw_strings_do_not_start_or_end_a_string(source: str, statement: str) -> None:
    assert classify(constant(source, "Lib.foo", "foo"), source).statement == statement


def test_crlf_sources_give_the_same_record_as_lf() -> None:
    source = "/-- Doc. -/\ntheorem t :\n    True := trivial\n"
    lf = classify(constant(source, "Lib.t", "t"), source)
    assert classify(constant(source, "Lib.t", "t"), source.replace("\n", "\r\n")) == lf


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("module\n\npublic import A\n", True),
        ("/-\nCopyright\n-/\nmodule\n", True),
        ("/- a /- nested -/ b -/ -- note\nmodule\n", True),
        ("import A\n", False),
        ("-- module\nimport A\n", False),
        ("module_doc\n", False),
        ("", False),
    ],
)
def test_a_module_file_starts_with_the_module_keyword(source: str, expected: bool) -> None:
    assert starts_with_module(source) is expected


def _line(marker: str, **record: object) -> str:
    return marker + json.dumps(record, ensure_ascii=False) + "\n"


def test_probe_output_is_decoded() -> None:
    marker = "M "
    text = (
        "noise\n"
        + _line(marker, record="module", name="Lib.A", module_doc="Header.")
        + _line(
            marker,
            record="constant",
            name="Lib.t",
            module="Lib.A",
            private=False,
            wrapper=None,
            range=[[1, 0], [1, 30]],
            selection=[[1, 8], [1, 9]],
            signature="True",
            docstring=None,
            mentions=["True"],
            axioms=[],
        )
        + _line(marker, record="shared", name="Lib.t", modules=["Lib.A", "Lib.B"])
    )
    modules, constants, shared = parse_probe_output(text, marker)
    assert modules == {"Lib.A": "Header."}
    assert shared == {"Lib.t": ("Lib.A", "Lib.B")}
    assert constants[0].range == ((1, 0), (1, 30))
    assert constants[0].mentions == ("True",)


@pytest.mark.parametrize(
    "line",
    [
        "M not json\n",
        'M {"record":"other"}\n',
        'M {"record":"module","name":"Lib.A"}\n',
        'M {"record":"module","name":"Lib.A","module_doc":null}\nM {"record":"module","name":"Lib.A","module_doc":null}\n',
        'M {"record":"constant","name":"Lib.t"}\n',
        # A field the generator does not read is not accepted in silence.
        'M {"record":"constant","name":"Lib.t","module":"Lib.A","constant":"theorem","private":false,"wrapper":null,'
        '"range":[[1,0],[1,30]],"selection":[[1,8],[1,9]],"signature":"True","docstring":null,"mentions":[],"axioms":[]}\n',
        'M {"record":"shared","name":"Lib.t"}\n',
        'M {"record":"shared","name":"Lib.t","modules":["Lib.A","Lib.B"],"extra":1}\n',
        'M {"record":"shared","name":"","modules":["Lib.A","Lib.B"]}\n',
        'M {"record":"shared","name":7,"modules":["Lib.A","Lib.B"]}\n',
        'M {"record":"shared","name":"Lib.t","modules":"Lib.A"}\n',
        'M {"record":"shared","name":"Lib.t","modules":["Lib.A"]}\n',
        'M {"record":"shared","name":"Lib.t","modules":["Lib.A","Lib.A"]}\n',
        'M {"record":"shared","name":"Lib.t","modules":["Lib.A",""]}\n',
        'M {"record":"shared","name":"Lib.t","modules":["Lib.A",3]}\n',
        'M {"record":"shared","name":"Lib.t","modules":["Lib.A","Lib.B"]}\n' * 2,
    ],
)
def test_malformed_probe_output_is_refused(line: str) -> None:
    with pytest.raises(ProbeOutputError):
        parse_probe_output(line, "M ")


def _constant_line(marker: str, docstring: str) -> str:
    return _line(
        marker,
        record="constant",
        name="Lib.t",
        module="Lib.A",
        private=False,
        wrapper=None,
        range=[[1, 0], [1, 30]],
        selection=[[1, 8], [1, 9]],
        signature="True",
        docstring=docstring,
        mentions=[],
        axioms=[],
    )


@pytest.mark.parametrize("character", ["\u2028", "\u0085", "\u2029"])
def test_a_unicode_line_separator_inside_a_record_does_not_split_it(character: str) -> None:
    docstring = f"before{character}after"
    _, constants, _ = parse_probe_output(_constant_line("M ", docstring), "M ")
    assert constants[0].docstring == docstring


def test_probe_output_with_crlf_line_endings_is_decoded() -> None:
    text = _constant_line("M ", "Doc.").replace("\n", "\r\n") + "noise\r\n"
    _, constants, _ = parse_probe_output(text, "M ")
    assert len(constants) == 1
    assert constants[0].docstring == "Doc."
