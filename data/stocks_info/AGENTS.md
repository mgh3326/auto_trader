# STOCKS INFO KNOWLEDGE BASE

## OVERVIEW
`data/stocks_info/` combines Python loader modules and static exchange reference `.h` assets for domestic/overseas code metadata.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Public exports and lazy loading | `data/stocks_info/__init__.py` | Package export surface for stock-code loaders |
| KIS KR market loaders | `kis_kospi_code_mst.py`, `kis_kosdaq_code_mst.py`, `kis_konex_code_mst.py` | KOSPI/KOSDAQ/KONEX master loaders |
| Overseas code loaders | `overseas_stock_code.py`, `overseas_us_stocks.py`, `overseas_nasdaq_code.py`, `overseas_index_code.py` | US/overseas symbol metadata |
| Futures/options/bonds loaders | `domestic_*_code.py`, `overseas_future_code.py` | Derivative and bond market code maps |
| Sector/theme/member metadata | `sector_code.py`, `theme_code.py`, `member_code.py` | Classification and member code support |
| Static vendor reference assets | `*.h` files in this directory | Raw source data used by loader/parsing workflows |

## CONVENTIONS
- Keep runtime parsing logic in Python loader modules, not in callsites.
- Treat `.h` files as immutable source assets unless source-data refresh is intended.
- Keep exports in `__init__.py` aligned with loader ownership and naming.
- Normalize output field shapes consistently across domestic/overseas loaders.

## ANTI-PATTERNS
- Do not rename/remove static `.h` assets without verifying dependent parser modules.
- Do not treat generated `__pycache__` artifacts as maintained source.
- Do not duplicate equivalent parsing logic across multiple loader files without clear boundary needs.
- Do not hardcode ad hoc symbol mappings in unrelated runtime modules when loaders already provide them.

## NOTES
- This directory is a mixed-content hotspot; isolate loader changes from raw-data asset updates in reviews.
