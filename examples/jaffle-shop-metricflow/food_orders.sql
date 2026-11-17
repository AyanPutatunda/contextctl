SELECT
  SUM(food_orders) AS food_orders
FROM (
  SELECT
    is_food_order AS order_id__is_food_order
    , 1 AS food_orders
  FROM "jaffle"."main"."orders" orders_src_10000
) subq_2
WHERE order_id__is_food_order = true
