"""shape_diff: the live table against the declared output columns."""
from continuo_engine_contract.port import LiveColumn

from continuo_python_runtime.contract.model import Column
from continuo_python_runtime.shape import NullabilityDiff, ShapeDiff, shape_diff

DECLARED = (
    Column("rate_date", "DATE", nullable=False),
    Column("base", "VARCHAR(3)", nullable=False),
    Column("rate", "DECIMAL(12,6)"),
)


def test_a_matching_table_has_no_difference():
    diff = shape_diff(
        DECLARED, [LiveColumn("rate_date", False), LiveColumn("base", False), LiveColumn("rate", True)]
    )
    assert not diff
    assert diff == ShapeDiff()


def test_column_order_is_ignored():
    live = [LiveColumn("rate", True), LiveColumn("base", False), LiveColumn("rate_date", False)]
    assert not shape_diff(DECLARED, live)


def test_names_compare_case_insensitively():
    live = [LiveColumn("RATE_DATE", False), LiveColumn("Base", False), LiveColumn("rate", True)]
    assert not shape_diff(DECLARED, live)


def test_missing_unexpected_and_nullability_are_all_reported():
    diff = shape_diff(
        DECLARED,
        [LiveColumn("rate_date", True), LiveColumn("base", False), LiveColumn("old_rate", True)],
    )
    assert diff.missing == ("rate",)
    assert diff.unexpected == ("old_rate",)
    assert diff.nullability == (NullabilityDiff("rate_date", declared=False, live=True),)
    assert diff


def test_the_exact_spelling_is_paired_first():
    declared = (Column("Rate", "BIGINT", nullable=False), Column("rate", "BIGINT"))
    assert not shape_diff(declared, [LiveColumn("rate", True), LiveColumn("Rate", False)])


def test_the_message_names_every_difference_and_the_remedy():
    diff = ShapeDiff(
        missing=("rate",),
        unexpected=("old_rate",),
        nullability=(NullabilityDiff("rate_date", declared=False, live=True),),
    )
    assert diff.message("fx.fx_rates") == (
        "table fx.fx_rates does not match the contract's output_columns: "
        "missing column(s) ['rate']; unexpected column(s) ['old_rate']; "
        "column rate_date is nullable: true in the table but nullable: false in the contract. "
        "An append or upsert table keeps its rows, so its shape cannot change: "
        "write to a new table (a _v2 name) under a new contract"
    )
