SELECT
  lifetime_spend_pretax / count_lifetime_orders AS average_order_value
FROM (
  SELECT
    SUM(count_lifetime_orders) AS count_lifetime_orders
    , SUM(lifetime_spend_pretax) AS lifetime_spend_pretax
  FROM "jaffle"."main"."customers" customers_src_10000
) subq_5
