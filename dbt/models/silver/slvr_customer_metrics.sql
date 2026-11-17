{{ config(
    materialized='view',
    schema='silver'
) }}

WITH customer_orders AS (
    SELECT
        o.customer_id,
        o.order_id,
        o.order_date,
        d.line_total as total_amount
    FROM {{ ref('brnz_orders') }} o
    INNER JOIN {{ ref('brnz_order_details') }} d
        ON o.order_id = d.order_id
)

SELECT
    customer_id,
    COUNT(order_id) as total_orders,
    SUM(total_amount) as lifetime_value,
    AVG(total_amount) as avg_order_value,
    MIN(order_date) as first_order_date,
    MAX(order_date) as last_order_date,
    DATEDIFF(day, MIN(order_date), MAX(order_date)) as customer_tenure_days
FROM customer_orders
GROUP BY customer_id
