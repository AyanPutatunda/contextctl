SELECT
  SUM(new_customer_orders) AS new_customer_orders
FROM (
  SELECT
    customer_order_number AS order_id__customer_order_number
    , 1 AS new_customer_orders
  FROM "jaffle"."main"."orders" orders_src_10000
) subq_2
WHERE order_id__customer_order_number = 1
