SELECT
  SUM(case when is_drink_item then product_price else 0 end) AS drink_revenue
FROM "jaffle"."main"."order_items" order_item_src_10000
