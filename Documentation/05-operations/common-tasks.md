# Common operational tasks

## Check cluster status

```bash
kubectl get ns
kubectl get pods -n devops-system
kubectl get pods -n gpu-operator
kubectl get pods -n monitoring
kubectl get crd | grep devops.local
```

## API access

```bash
kubectl port-forward -n devops-system svc/devops-api 8000:8000
curl http://localhost:8000/gpu/list
```

## Login and use token

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/login \
  -H "Content-Type: application/json" \
  -d '{"username":"<username>","password":"<password>"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')

curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/reservations/calendar
```

## Create a user namespace manually

```bash
kubectl create namespace user-alice
kubectl label namespace user-alice devops.dev/user=alice
kubectl label namespace user-alice devops.dev/user-namespace=true
```

## Submit a Task CR directly

```bash
kubectl apply -f devops-backend/examples/test-gpu-job.yaml
kubectl get tasks -n user-testuser
kubectl get pods -n user-testuser
```

Edit the namespace/user in the example before applying.

## List tasks

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/tasks
```

## Delete a task

```bash
curl -X DELETE -H "Authorization: Bearer $TOKEN" http://localhost:8000/tasks/task-abc123
```

## Schedule a job

```bash
curl -X POST http://localhost:8000/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "gpu_uuid": "gpu-1",
    "start_time": "<start-time>",
    "end_time": "<end-time>",
    "image": "alpine:latest",
    "command": ["echo", "scheduled job"]
  }'
```

## List jobs

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/jobs
```

## Cancel a scheduled job

```bash
curl -X DELETE -H "Authorization: Bearer $TOKEN" http://localhost:8000/jobs/1
```

## Check task logs

```bash
kubectl get pods -n user-<username>
kubectl logs -n user-<username> <pod-name>
```

## Check controller logs

```bash
kubectl logs -n devops-system deployment/devops-controller
```

## Check API logs

```bash
kubectl logs -n devops-system deployment/devops-api
```

## Check webhook logs

```bash
kubectl logs -n devops-system deployment/vram-webhook
```

## Check PostgreSQL

```bash
kubectl exec -it -n devops-system deployment/postgres -- psql -U postgres devops
```

Useful SQL:

```sql
SELECT id, username, role FROM users;
SELECT * FROM reservations;
SELECT * FROM jobs ORDER BY start_time DESC LIMIT 10;
SELECT * FROM tasks ORDER BY created_at DESC LIMIT 10;
SELECT * FROM gpu_metrics ORDER BY timestamp DESC LIMIT 10;
```

Note: `tasks` table is only populated in local dev mode. In normal operation, tasks are Kubernetes custom resources.

## Access Grafana

```bash
kubectl port-forward -n monitoring svc/monitoring-grafana 3000:80
```

Then open `http://localhost:3000`.
