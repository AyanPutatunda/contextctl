SELECT
  SUM(case when is_food_item then product_price else 0 end) AS food_revenue
FROM "jaffle"."main"."order_items" order_item_src_10000
