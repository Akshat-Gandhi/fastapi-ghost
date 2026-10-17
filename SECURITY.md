# Security

Please report vulnerabilities privately through GitHub's "Report a vulnerability" (Security tab) rather than a public issue.

What Ghost protects, and what it asks of you:

- It sends requests to servers you point it at. Run it against a dev or sandbox copy, never production. Writes (POST/PUT/PATCH/DELETE) are off by default and run only on `localhost` or hosts you list in `allowed_hosts`.
- `api.toml` holds environment variable *names*; login tokens and captured values stay in memory. Stored responses are redacted and scrubbed of known secret values.
- The web app stores passwords as salted scrypt hashes, sessions hashed, and project headers encrypted with `GHOST_SECRET_KEY`. A fresh install has an `admin` / `admin` account that must be changed at first sign-in; set `GHOST_ADMIN_PASSWORD` to avoid the default. The UI binds to `127.0.0.1` in the supplied Docker setup; put it behind TLS if you expose it.
- Mount source code read-only (`./sources` is mounted `:ro`).
