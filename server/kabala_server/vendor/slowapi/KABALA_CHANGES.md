# slowapi 0.1.10 in Kabala

Source: https://github.com/laurents/slowapi (MIT, see `LICENSE`), copied from the PyPI wheel.
The first commit that added this folder is the original, unchanged; every change since is marked `# Kabala:` in the code.

| File | Change | Why |
|---|---|---|
| `util.py` | `client_ip()` trusts only the right-most `proxy_hops` entries of `X-Forwarded-For`; `get_ipaddr` uses it | The original returned the header as-is, so an attacker could send a new fake IP with every request and never be limited |
| `extension.py` | `Config(config_filename)` only | The original silently read `./.env`, so a stray file could change or turn off limits |
| `extension.py` | 429 body is `{"detail": …}` | Same shape as every other error of the Kabala API |
| `extension.py` | `slowapi_startup(app)` | The original referenced an undefined `app` |
| `extension.py` | Decorated routes without a `response` parameter skip header injection | The original raised an exception there when `headers_enabled=True` |
| `middleware.py` | Relative import | The package lives under `kabala_server.vendor` |

Needs the `limits` package (MIT, the storage and algorithms behind slowapi and flask-limiter).
