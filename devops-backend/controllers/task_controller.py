#!/usr/bin/env python3
import kopf
import kubernetes
import logging
from datetime import datetime, timedelta
import yaml
import os
import uuid

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def create_pod_from_task(spec, namespace, task_name):
    """Tworzy pod na podstawie definicji Task"""
    api = kubernetes.client.CoreV1Api()
    
    # Budujemy env: bazowe + (opcjonalnie) VRAM + dodatkowe z spec.env (koordynacja multi-node/MPI).
    env = [
        {"name": "USER_NAME", "value": spec['user']},
        {"name": "PROJECT_NAME", "value": spec.get('project', 'default')},
    ]
    if spec.get('vramLimitGB') is not None:
        env.append({"name": "VRAM_LIMIT_MB", "value": str(int(spec['vramLimitGB']) * 1024)})
    for extra in spec.get('env', []) or []:
        if isinstance(extra, dict) and 'name' in extra:
            env.append({"name": extra['name'], "value": str(extra.get('value', ''))})

    labels = {"app": "task", "task-name": task_name, "user": spec['user']}
    if spec.get('jobGroup'):
        labels['job-group'] = str(spec['jobGroup'])
    if spec.get('project'):
        labels['project'] = str(spec['project'])   # do egzekwowania limitów per-projekt
    if spec.get('gpuType'):
        labels['gpu-type'] = str(spec['gpuType'])  # do liczenia limitów per-typ GPU

    # Wyniki: montujemy trwały PVC użytkownika (scratch) pod /results/<task_name>,
    # aby pliki wyjściowe przeżyły usunięcie poda i były dostępne do pobrania przez API.
    results_dir = os.getenv("RESULTS_DIR", "/results")
    results_pvc = f"{spec['user']}-{os.getenv('RESULTS_PVC_SUFFIX', 'scratch')}"
    # subPath=task_name już izoluje katalog na PVC, więc zadanie pisze wprost do results_dir.
    env.append({"name": "RESULTS_DIR", "value": results_dir})

    pod_manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": f"task-{task_name}",
            "labels": labels
        },
        "spec": {
            "restartPolicy": "Never",
            "containers": [{
                "name": "main",
                "image": spec['image'],
                "command": spec.get('command', ['sleep', 'infinity']),
                "resources": spec.get('resources', {}),
                "env": env,
                "volumeMounts": [{
                    "name": "results",
                    "mountPath": results_dir,
                    "subPath": task_name
                }]
            }],
            "volumes": [{
                "name": "results",
                "persistentVolumeClaim": {"claimName": results_pvc}
            }],
            "imagePullSecrets": [{"name": "regcred"}] if spec.get('imagePullSecrets') else []
        }
    }
    
    # Slice binding: jeśli zadanie celuje w profil MIG, żądamy zasobu nvidia.com/mig-<profil>
    # zamiast całej karty nvidia.com/gpu (Scheduler umieści pod na pasującym slice).
    mig_profile = spec.get('migProfile')
    if mig_profile:
        c = pod_manifest['spec']['containers'][0]
        c.setdefault('resources', {})
        c['resources'].setdefault('limits', {})
        # usuwamy ewentualne żądanie całej karty i wstawiamy konkretny slice MIG
        c['resources']['limits'].pop('nvidia.com/gpu', None)
        c['resources']['limits'][f"nvidia.com/mig-{mig_profile}"] = 1

    gpu_limit = spec.get('resources', {}).get('limits', {}).get('nvidia.com/gpu', 0)
    if gpu_limit > 0:
        if spec.get('gpuUUID'):
            pod_manifest['spec']['nodeSelector'] = {
                f"nvidia.com/gpu.uuid.{spec['gpuUUID']}": "true"
            }
    
    try:
        api.create_namespaced_pod(namespace=namespace, body=pod_manifest)
        return pod_manifest['metadata']['name']
    except kubernetes.client.exceptions.ApiException as e:
        logger.error(f"Failed to create pod: {e}")
        raise

@kopf.on.create('devops.local', 'v1', 'tasks')
def task_create(spec, name, namespace, **kwargs):
    logger.info(f"Creating task {name} in namespace {namespace}")
    pod_name = create_pod_from_task(spec, namespace, name)
    return {
        'podName': pod_name,
        'phase': 'Running',
        'startTime': datetime.utcnow().isoformat()
    }

def _delete_pod_quietly(namespace, pod_name):
    api = kubernetes.client.CoreV1Api()
    try:
        api.delete_namespaced_pod(pod_name, namespace)
        logger.info(f"Deleted pod {pod_name} in {namespace}")
        return True
    except kubernetes.client.exceptions.ApiException as e:
        if e.status == 404:
            return True
        logger.error(f"Failed to delete pod {pod_name}: {e}")
        return False

@kopf.on.timer('devops.local', 'v1', 'tasks', interval=30)
def check_timeout(spec, status, meta, name, namespace, **kwargs):
    """Usuwa pod zadania po przekroczeniu timeLimitSeconds (liczone od utworzenia Taska)."""
    time_limit = spec.get('timeLimitSeconds')
    if not time_limit:
        return
    # Liczymy czas od creationTimestamp CR-a (status bywa pusty), z fallbackiem na status.startTime.
    start = None
    created = meta.get('creationTimestamp')
    if created:
        try:
            start = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            start = None
    if start is None:
        start_time = (status.get('task_create') or {}).get('startTime') or status.get('startTime')
        if start_time:
            try:
                start = datetime.fromisoformat(start_time)
            except ValueError:
                start = None
    if start is None:
        return
    if datetime.utcnow() - start > timedelta(seconds=time_limit):
        logger.warning(f"Task {name} exceeded time limit; deleting pod")
        # Pod ma deterministyczną nazwę task-<name>; usuwamy też po niej.
        _delete_pod_quietly(namespace, f"task-{name}")

@kopf.on.delete('devops.local', 'v1', 'tasks')
def task_delete(spec, status, name, namespace, **kwargs):
    """Czyści pod po usunięciu Task (pod ma deterministyczną nazwę task-<name>)."""
    # status.podName bywa pusty (kopf zapisuje zwrotkę pod kluczem handlera), więc używamy konwencji nazwy.
    pod_name = (status.get('task_create') or {}).get('podName') or status.get('podName') or f"task-{name}"
    _delete_pod_quietly(namespace, pod_name)
