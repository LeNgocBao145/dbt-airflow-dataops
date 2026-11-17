select
    sales_order_id,
    order_detail_id,
    order_date
from {{ ref('slvr_sales_orders') }}
where order_date > cast(getdate() as date)
