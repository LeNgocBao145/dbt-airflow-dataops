{{
    config(
        materialized='view'
    )
}}

select
    SalesOrderDetailID as order_detail_id,
    SalesOrderID as order_id,
    ProductID as product_id,
    OrderQty as quantity,
    UnitPrice as unit_price,
    UnitPriceDiscount as unit_price_discount,
    LineTotal as line_total
from {{ source('adventureworks', 'SalesOrderDetail') }}
