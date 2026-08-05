def run(ctx):
    """Return the node's output; the harness conforms and writes it.

    Each declared read is fetched once, memoized, and returned as a
    pyarrow.Table. Combining them is ordinary Python — scripts never write
    SQL and never touch a warehouse driver.
    """
    orders = ctx.read("orders")
    customers = ctx.read("customers")

    joined = orders.join(customers, keys="customer_id", join_type="left outer")

    # Project to the declared output_columns, in order and under their
    # declared names; conform() then enforces types and nullability.
    return joined.select(["order_id", "name", "amount"]).rename_columns(
        ["order_id", "customer_name", "amount"]
    )
