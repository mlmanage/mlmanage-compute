# Bootstrap the first admin

The first admin is created **automatically** on API startup — no manual step is required.

## Automatic bootstrap (default)

When the API starts and finds the `users` table empty, it:

1. Creates an admin user with role `admin` and the configured bootstrap credentials.
2. Creates the `user-admin` namespace, labeled `devops.dev/user=admin` and `devops.dev/user-namespace=true`.
3. Creates the admin's workspace PVCs (`admin-home`, `admin-scratch`, `admin-project`).

So after deploying with the scripts, the admin can log in **and** run tasks immediately.

```bash
curl -X POST http://localhost:8000/login \
  -H "Content-Type: application/json" \
  -d '{"username":"<bootstrap-username>","password":"<bootstrap-password>"}'
```

The bootstrap is **idempotent**: it runs only when no users exist, and never overwrites existing data.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `BOOTSTRAP_ADMIN` | `true` | Enable/disable automatic admin bootstrap. |
| `BOOTSTRAP_ADMIN_USERNAME` | Insecure development value | Username for the bootstrapped admin. |
| `BOOTSTRAP_ADMIN_PASSWORD` | Insecure development value | Password for the bootstrapped admin. |

Set both credential variables from a Kubernetes Secret before the first start, or set `BOOTSTRAP_ADMIN=false` to skip bootstrap.

## Manual creation (optional fallback)

Do not insert a plaintext value into `hashed_password`; login deliberately rejects it. Prefer starting once with `BOOTSTRAP_ADMIN=true`, or generate a PBKDF2 value with the API module's `hash_password()` helper before inserting it. A manual insert does **not** create the namespace/workspaces, so create them yourself:

```bash
kubectl create namespace user-admin
kubectl label namespace user-admin devops.dev/user=admin
kubectl label namespace user-admin devops.dev/user-namespace=true
```

## Security note

Passwords are stored as salted PBKDF2-HMAC-SHA256 hashes. The bootstrap password and JWT secret must still come from secret management in production; do not use the documented development defaults.
