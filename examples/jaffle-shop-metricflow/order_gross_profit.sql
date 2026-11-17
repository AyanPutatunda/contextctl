SELECT
  revenue - cost AS order_gross_profit
FROM (
  SELECT
    MAX(subq_5.revenue) AS revenue
    , MAX(subq_11.cost) AS cost
  FROM (
    SELECT
      SUM(product_price) AS revenue
    FROM "jaffle"."main"."order_items" order_item_src_10000
  ) subq_5
  CROSS JOIN (
    SELECT
      SUM(order_cost) AS cost
    FROM "jaffle"."main"."orders" orders_src_10000
  ) subq_11
) subq_12
