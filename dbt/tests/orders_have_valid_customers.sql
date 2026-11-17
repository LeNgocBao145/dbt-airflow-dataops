select
    o.sales_order_id,
    o.order_detail_id,
    o.customer_id
from {{ ref('slvr_sales_orders') }} as o
left join {{ ref('slvr_customers') }} as c
    on o.customer_id = c.customer_id
where c.customer_id is null
