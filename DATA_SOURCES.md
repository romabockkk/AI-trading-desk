
- Use read-only permissions wherever possible.
- Demo trading credentials must be separated from future live credentials.
- Never enable withdrawal permissions.

## 8. Failure Handling

If a data source becomes unavailable:
- Notify Head Trader.
- Mark the affected data as unavailable.
- Do not generate trade signals from missing data.
- Resume analysis only after data quality is verified.

## 9. Important Rule

Every trading decision must be traceable to actual market data and its source.

If reliable data is unavailable, the final decision must be NOT ENOUGH DATA.
