# Known Risks — Earnings Report v1

1. **Classifier accuracy is load-bearing.** The signal blindly trusts the
   upstream catalyst classifier's `(category="earnings", subcategory="report")`
   labels. A misclassification (guidance, pre-announcement, or unrelated PR)
   directly produces bad entries. There is no defensive re-validation here.

2. **Alpaca news latency may eat the edge window.** The 60m validated horizon
   is measured from event timestamp. End-to-end latency (Alpaca push → fetcher
   → classifier → bus → entry fill) can consume a meaningful fraction of the
   window. If realized latency exceeds ~10–15 minutes the residual edge may
   collapse below break-even after costs.

3. **Survivorship bias in validation universe.** The 5.09× @ 60m, N=33 figure
   was measured against the 2024 mid-cap universe as constituted today, not
   as constituted at each bar. Names that delisted, were acquired, or fell
   out of the cap band during 2024 are under-represented. Live performance
   on the actual then-current universe may be lower.

4. **2024 vol regime specificity.** N=33 is a single-year, single-regime
   sample. The 2024 environment featured specific vol, breadth, and rate
   conditions that drove post-earnings continuation. A regime with stronger
   mean-reversion (e.g. high-VIX, tight breadth) could invert the edge sign
   without warning, and the small-N validation cannot distinguish that case
   from noise.
