# Vendored browser libraries (monitoring web interface)

Served locally under `/monitor/static/vendor/` - no CDN. Files are unmodified
copies from the npm packages; the npm `dist.integrity` (SHA-512) of each
tarball was verified before extraction (26.11.2026). Chart.js contains no
`eval`/`new Function` and runs under the dashboard CSP without `unsafe-eval` or
`unsafe-inline` (verified by the browser tests).

| Library | Version | Files | License | SHA-256 |
|---|---|---|---|---|
| Bootstrap | 5.3.3 | `bootstrap/bootstrap.min.css` | MIT (`bootstrap/LICENSE`) | `3c8f27e6009ccfd710a905e6dcf12d0ee3c6f2ac7da05b0572d3e0d12e736fc8` |
| Bootstrap | 5.3.3 | `bootstrap/bootstrap.bundle.min.js` (incl. Popper) | MIT | `0833b2e9c3a26c258476c46266e6877fc75218625162e0460be9a3a098a61c6c` |
| Chart.js | 4.4.7 | `chartjs/chart.umd.js` (UMD build, unmodified) | MIT (`chartjs/LICENSE.md`) | `2812cb8825fdc57469eb2f7bb055e9429244e599920511ee477e828499b632cb` |
| Bootstrap Icons | 1.11.3 | 25 icons as inline-SVG subset in `../../assets/icons.json` (rendered server-side, not served) | MIT (`../../assets/bootstrap-icons-LICENSE`) | `a4cf87b82cc557f0d39bad8083698e778955e7aeb52bf2af3c97058d0fccb0a3` (subset file) |

Only `.css` and `.js` files below `static/` are served; this README is not.
Update procedure: download the npm tarball, verify `dist.integrity`, copy the
same files, update this table and run the browser test
(`tests/test_monitor_web_browser.py`).
