with customer_metrics as (
    select
        customer_id,
        total_revenue as lifetime_value
    from {{ ref('gld_customer_metrics') }}
)

select
    customer_id,
    lifetime_value
from customer_metrics
where lifetime_value is not null
  and lifetime_value <= 0
