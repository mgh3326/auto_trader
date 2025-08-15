import pandas as pd

from app.core.config import settings
from app.services.kis import kis
from app.services.telegram import send


async def screen_once_async():
    raw = await kis.volume_rank()
    df = pd.DataFrame(raw).astype({"prdy_ctrt": float, "acml_vol": int})
    sel = df.query("prdy_ctrt <= @settings.drop_pct").nlargest(
        settings.top_n, "acml_vol"
    )
    if sel.empty:
        return
    for _, r in sel.iterrows():
        msg = (
            f"*{r.hts_kor_isnm}* `{r.mksc_shrn_iscd}`\n"
            f"▼{r.prdy_ctrt:+.2f}% · 거래량 {r.acml_vol:,}"
        )
        await send(msg)
