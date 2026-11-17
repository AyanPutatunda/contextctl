SELECT
  PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY (product_price)) AS median_revenue
FROM "jaffle"."main"."order_items" order_item_src_10000
