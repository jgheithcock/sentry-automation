**Sentry issue:** [BILLING-1A](https://example.sentry.io/issues/0000000000/)
**Sentry issue ID:** `0000000000`
**Culprit:** `billing.amounts in parse_amount`
**Events:** 17 · **Users:** 4
**First seen:** 2026-11-20T10:00:00Z · **Last seen:** 2026-11-20T12:00:00Z

### Stack trace (latest event)
```
  File "billing/amounts.py", line 13, in invoice_total
    subtotal = sum(parse_amount(item) for item in line_items)
  File "billing/amounts.py", line 13, in <genexpr>
    subtotal = sum(parse_amount(item) for item in line_items)
  File "billing/amounts.py", line 8, in parse_amount
    return float(text)
ValueError: could not convert string to float: '1,234.50'
```

_Fake Sentry issue for testing sentry-autofix; there is no real event to fetch._
