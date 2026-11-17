SELECT
  CAST(SUM(case when is_drink_item then product_price else 0 end) AS DOUBLE) / CAST(NULLIF(SUM(product_price), 0) AS DOUBLE) AS drink_revenue_pct
FROM "jaffle"."main"."order_items" order_item_src_10000
