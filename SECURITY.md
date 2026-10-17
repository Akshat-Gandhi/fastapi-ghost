# Security

Please report vulnerabilities privately through GitHub's "Report a vulnerability" (Security tab) rather than a public issue.

What Ghost protects, and what it asks of you:

- It sends requests to servers you point it at. Run it against a dev or sandbox copy, never production. Writes (POST/PUT/PATCH/DELETE) are off by default and run only on `localhost` or hosts you list in `allowed_hosts`.
- `api.toml` holds environment variable *names*; login tokens and captured values stay in memory. Stored responses are redacted and scrubbed of known secret values.
- The web app stores passwords as salted scrypt hashes, sessions hashed, and project headers encrypted with `GHOST_SECRET_KEY`. A fresh install has an `admin` / `admin` account that must be changed at first sign-in; set `GHOST_ADMIN_PASSWORD` to avoid the default. The UI binds to `127.0.0.1` in the supplied Docker setup; put it behind TLS if you expose it.
- Mount source code read-only (`./sources` is mounted `:ro`).

## Who can point Ghost at what

Enrolling a project makes the **server** send requests to that URL (including `localhost`, which is the host machine when Ghost runs in Docker). Anyone who can sign in can therefore aim Ghost at anything the server can reach: other containers, private networks, cloud metadata addresses. Treat accounts as trusted operators. On a shared or exposed instance set `GHOST_ALLOW_SIGNUP=0` (new accounts are then created by an admin only), keep the UI behind TLS and authentication, and run it on a network that cannot reach internal services you would not want probed.
