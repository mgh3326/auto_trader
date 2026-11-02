# ROB-1474 diagnostic v1 — superseded calendar convention

Experiment-Manifest-Sha256: 58dadaf8247a284f348cd02075192adcbfa86dceb408298a5abf424a9ed72080
Experiment-Test-Look: yes

SURVIVOR-ONLY / BOUNDED historical diagnostic, k=1. This preliminary view used a 252-session high for every market, which is approximately 52 weeks in equities but only 36 weeks in 24/7 crypto. Corrected to 364 daily sessions for crypto in v2 before publication/READY; no model was selected from these results. This frozen record counts the preliminary view rather than erasing it. No readiness or alpha verdict.

| Market | h | Ranker IC | Highest baseline IC (descriptive) | Baseline | Evaluation JSON SHA-256 |
|---|---:|---:|---:|---|---|
| kr | 1 | -0.010885 | 0.034699 | gap | `918a66d1164be820094776d9390ff9a764b59dc53c72193b383243da07d1c52b` |
| kr | 3 | -0.034078 | 0.044666 | relative_volume_shock | `5114c908c62895c513ba2280fb0fd2892402f5522b0f08b696fdc2d51bf2f689` |
| kr | 5 | -0.036176 | 0.031432 | gap | `874c18a6ed102ecbf292de338214bc02e7af73be5d7f517f768fc227b5cd91f8` |
| kr | 10 | 0.007623 | 0.030096 | gap | `f163b10d7a090198228c1cf902bb625c4c25d4021a780136146a3167e43f7a8f` |
| us | 1 | 0.006953 | 0.048509 | high_distance_252 | `7bd5def9c150507511cc01d9cef7fb4ddffeb0a7bb1b05a5cb709b1069694430` |
| us | 3 | 0.035252 | 0.074807 | relative_volatility_20 | `40f19ddfe4b89a22c63e01cee6f981fea62375f972cfd56db427d07212080794` |
| us | 5 | 0.016094 | 0.073737 | relative_volatility_20 | `c7e270499c8804e8f14e968b8f3dd0723b0719789a0c11f84fd4bc57e614d404` |
| us | 10 | 0.097282 | 0.103237 | relative_volatility_20 | `6d6e16c649b5a20dc5c3c1e57424585223e7fdd4e2c10f70977fbb33ba499fba` |
| crypto | 1 | 0.067017 | 0.061079 | relative_volatility_20 | `6e7c2141994fa1a2a52850580451286c501f7c4a4cca4f131deb9ecfe6e93cef` |
| crypto | 3 | 0.034952 | 0.072547 | relative_volatility_20 | `99ce14f4e3aae978e3d363cbe74b7505b79c5011b993dc44d4db2df1fb367988` |
| crypto | 5 | 0.021376 | 0.085784 | relative_volatility_20 | `8f9b82d410ffde3b1fc5f6ce1e2d94ee2329444d68b202b9755555d23a2f8869` |
| crypto | 10 | 0.081090 | 0.102964 | relative_volatility_20 | `bea660cf4e829ebc98b15d614788114bd107bb99d7abc68fdfd78b14812200f0` |
