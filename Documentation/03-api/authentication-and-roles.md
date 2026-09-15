# Authentication and roles

## JWT authentication

Protected endpoints use `HTTPBearer`. The dependency:

1. Reads bearer token.
2. Decodes with `SECRET_KEY` and `HS256`.
3. Reads username from `sub`.
4. Loads the user row from PostgreSQL.
5. Rejects missing/invalid tokens or missing users with HTTP 401.

Tokens are created by `/login` and expire after 24 hours. Passwords are stored as salted PBKDF2-HMAC-SHA256 hashes. Login audit records contain only the username and `user_found`/`password_correct` booleans; password values are never logged.

## Role checks

`check_role(allowed_roles)` is used as a FastAPI dependency. If `current_user.role` is not in the allowed list, the API returns HTTP 403.

Current protected endpoints:

| Endpoint | Allowed roles |
|---|---|
| `GET /me` | Any authenticated user |
| `GET /users` | `admin` |
| `POST /users` | `admin` |
| `POST /reservations` | `user`, `poweruser`, `admin` |
| `GET /tasks` | `user`, `poweruser`, `admin`, `readonly` |
| `POST /tasks` | `user`, `poweruser`, `admin` |
| `DELETE /tasks/{task_name}` | `user`, `poweruser`, `admin` |
| `POST /images`, `PUT /images/{id}`, `DELETE /images/{id}` | `user`, `poweruser`, `admin` |
| `GET /images` | `user`, `poweruser`, `admin`, `readonly` |
| `GET /tasks/{task}/logs`, `GET /tasks/{task}/results`, `GET /tasks/{task}/results/download` | `user`, `poweruser`, `admin`, `readonly` |
| `DELETE /tasks/{task}/results` | `user`, `poweruser`, `admin` |
| `POST /jobs` | `user`, `poweruser`, `admin` |
| `GET /jobs` | `user`, `poweruser`, `admin`, `readonly` |
| `DELETE /jobs/{job_id}` | `user`, `poweruser`, `admin` |
| `POST /queue`, `DELETE /queue/{id}` | `user`, `poweruser`, `admin` |
| `GET /queue` | `user`, `poweruser`, `admin`, `readonly` |
| `GET /gpu/capabilities`, `GET /gpu/partitions` | `user`, `poweruser`, `admin`, `readonly` |
| `GET /disk/usage`, `GET /analytics/usage`, `GET /gpu/usage` | Any authenticated user |
| `GET /gpu/usage/{username}` | `admin` |
| `PUT /users/{username}` | `admin` |
| `POST /teams/{team}/shared-storage` | `admin`, `poweruser` |
| `GET /settings/cleanup`, `PUT /settings/cleanup` | `admin` |

`WS /tasks/{task_name}/exec` authenticates via a `token` query parameter (JWT) instead of the `Authorization` header, then enforces the same `user`/`poweruser`/`admin` roles.

Current public endpoints include `/health`, `/gpu/list`, `/reservations/calendar`, `/reservations/calendar.ics`, and `/metrics`.

## Kubernetes RBAC vs API roles

There are two authorization layers:

1. API application roles stored in PostgreSQL.
2. Kubernetes RBAC manifests under `devops-backend/rbac/` and service-account permissions in deployment YAMLs.

The FastAPI app does not automatically bind Kubernetes users to the ClusterRoles in `cluster-roles.yaml` when creating an API user.

## Security caveats

- Passwords are hashed and login accepts a JSON body rather than URL query credentials; production deployments must still use TLS and avoid logging request bodies.
- Deployment manifests contain an insecure static JWT signing value.
- Public endpoints may leak cluster or reservation information.
- Bootstrap credentials have insecure development defaults.

## Recommended improvements

- Periodically review the configured PBKDF2 work factor and provide a deliberate password-reset/rehash process before changing formats.
- Configure bootstrap credentials through a Kubernetes Secret before the first API startup.
- Move `SECRET_KEY` to a Kubernetes Secret.
- Require auth for GPU, calendar, usage, and metrics endpoints as appropriate.
- Add token revocation/rotation strategy if needed.
