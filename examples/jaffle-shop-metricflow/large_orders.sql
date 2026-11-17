SELECT
  SUM(large_orders) AS large_orders
FROM (
  SELECT
    order_total AS order_id__order_total_dim
    , 1 AS large_orders
  FROM "jaffle"."main"."orders" orders_src_10000
) subq_2
WHERE order_id__order_total_dim >= 20
