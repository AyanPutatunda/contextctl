SELECT
  SUM(drink_orders) AS drink_orders
FROM (
  SELECT
    is_drink_order AS order_id__is_drink_order
    , 1 AS drink_orders
  FROM "jaffle"."main"."orders" orders_src_10000
) subq_2
WHERE order_id__is_drink_order = true
