import hashlib
import hmac
import io
import json
import os
import secrets
import tarfile
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Literal, Optional
from fastapi import FastAPI, Depends, HTTPException, Request, status, UploadFile, File, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, String, Integer, DateTime, Float, Boolean, Text, or_
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from starlette.routing import Match
import kubernetes
import jwt
import prometheus_client
import logging
import threading
import time
import requests
import subprocess
import shutil
import tempfile
import re
import asyncio
from contextvars import ContextVar
from kubernetes.stream import stream as k8s_stream

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@postgres/devops")
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")
ALGORITHM = "HS256"
LOCAL_DEV_MODE = os.getenv("LOCAL_DEV_MODE", "false").lower() in {"1", "true", "yes"}
LOCAL_DEV_GPUS = os.getenv("LOCAL_DEV_GPUS")
CORS_ORIGINS = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "*").split(",") if origin.strip()]

# Domyślny limit VRAM (GB) dla zadań, które nie podają własnego.
DEFAULT_VRAM_LIMIT_GB = int(os.getenv("DEFAULT_VRAM_LIMIT_GB", "4"))
# When unset, storageClassName is omitted so Kubernetes uses the cluster's default StorageClass.
WORKSPACE_STORAGE_CLASS = os.getenv("WORKSPACE_STORAGE_CLASS") or None
# Recreate pending workspace PVCs that reference a StorageClass unavailable in the cluster.
# Disable with HEAL_WORKSPACE_PVCS=false.
HEAL_WORKSPACE_PVCS = os.getenv("HEAL_WORKSPACE_PVCS", "true").lower() in {"1", "true", "yes"}
DEFAULT_DISK_HOME_GB = int(os.getenv("DEFAULT_DISK_HOME_GB", "10"))
DEFAULT_DISK_SCRATCH_GB = int(os.getenv("DEFAULT_DISK_SCRATCH_GB", "20"))
DEFAULT_DISK_PROJECT_GB = int(os.getenv("DEFAULT_DISK_PROJECT_GB", "10"))
# Partycjonowanie GPU (hardware-agnostyczne: time-slicing / MPS / MIG).
# ConfigMap device-plugina, którą edytujemy aby ustawić per-node time-slicing.
GPU_OPERATOR_NS = os.getenv("GPU_OPERATOR_NS", "gpu-operator")
TIME_SLICING_CONFIGMAP = os.getenv("TIME_SLICING_CONFIGMAP", "time-slicing-config")
# Własna konfiguracja mig-parted pozwalająca na RÓŻNĄ geometrię MIG per-GPU na tym samym węźle.
MIG_PARTED_CONFIGMAP = os.getenv("MIG_PARTED_CONFIGMAP", "custom-mig-parted-config")
MIG_PARTED_KEY = os.getenv("MIG_PARTED_KEY", "config.yaml")
# Profile MIG rozpoznawane jako "VRAM slicing" (tylko sprzęt MIG: A100/A30/H100...).
# Mapowanie etykiety profilu -> przybliżony VRAM (GB) na slice, dla walidacji/wyświetlania.
MIG_PROFILE_VRAM_GB = {
    "1g.5gb": 5, "1g.10gb": 10, "1g.20gb": 20,
    "2g.10gb": 10, "2g.20gb": 20, "3g.20gb": 20,
    "3g.40gb": 40, "4g.20gb": 20, "7g.40gb": 40, "7g.80gb": 80,
}

# Kolektor metryk GPU (Prometheus/DCGM -> tabela gpu_metrics).
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://monitoring-kube-prometheus-prometheus.monitoring:9090")
GPU_METRICS_INTERVAL = int(os.getenv("GPU_METRICS_INTERVAL", "60"))
GPU_METRICS_ENABLED = os.getenv("GPU_METRICS_ENABLED", "true").lower() in {"1", "true", "yes"}

# Rejestr obrazów: adres, pod który API wypycha obrazy (wewnątrz klastra),
# oraz prefiks, którego używają pody zadań do pobrania obrazu (proxy na węźle).
REGISTRY_PUSH_HOST = os.getenv("REGISTRY_PUSH_HOST", "registry.kube-system.svc.cluster.local:80")
REGISTRY_PULL_PREFIX = os.getenv("REGISTRY_PULL_PREFIX", "localhost:5000")
CRANE_PATH = os.getenv("CRANE_PATH", "crane")
MAX_IMAGE_UPLOAD_MB = int(os.getenv("MAX_IMAGE_UPLOAD_MB", "8192"))

# Sprzątanie zasobów (reaper): TTL dla zadań i częstotliwość pętli czyszczącej.
# Wartości env to tylko domyślne przy starcie; są nadpisywalne w czasie działania przez API /settings/cleanup.
CLEANUP_ENABLED = os.getenv("CLEANUP_ENABLED", "true").lower() in {"1", "true", "yes"}
CLEANUP_SETTINGS = {
    "cleanup_interval_seconds": int(os.getenv("CLEANUP_INTERVAL", "60")),                    # co ile działa reaper
    "standalone_task_ttl_seconds": int(os.getenv("STANDALONE_TASK_TTL_SECONDS", str(24 * 3600))),  # zadania bez joba: 1 dzień
    "result_ttl_seconds": int(os.getenv("RESULT_TTL_SECONDS", str(7 * 24 * 3600))),          # wyniki zadań: 7 dni
    # Pody results-helper są tworzone na żądanie i siedzą bezczynnie (sleep 86400);
    # reaper usuwa te nieużywane dłużej niż ten TTL (domyślnie 30 min), by nie
    # zostawały w każdym namespace użytkownika. Tworzą się ponownie przy następnym żądaniu.
    "results_helper_idle_ttl_seconds": int(os.getenv("RESULTS_HELPER_IDLE_TTL_SECONDS", "1800")),
}
# Adnotacja stemplująca ostatnie użycie poda-pomocnika (do reapingu bezczynnych).
HELPER_LAST_USED_ANNOTATION = "devops.local/last-used"

# Bootstrap pierwszego administratora przy starcie (gdy baza nie ma użytkowników).
# Dzięki temu system działa od razu po deployu skryptami, bez ręcznego INSERT-a do bazy.
BOOTSTRAP_ADMIN = os.getenv("BOOTSTRAP_ADMIN", "true").lower() in {"1", "true", "yes"}
BOOTSTRAP_ADMIN_USERNAME = os.getenv("BOOTSTRAP_ADMIN_USERNAME", "admin")
BOOTSTRAP_ADMIN_PASSWORD = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "admin")

# Powiadomienia (e-mail/Slack) o zbliżających się początku/końcu rezerwacji.
# Wszystko opcjonalne: bez skonfigurowanych poświadczeń funkcje działają jako no-op.
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "mlmanage@localhost")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
NOTIFY_LEAD_SECONDS = int(os.getenv("NOTIFY_LEAD_SECONDS", "600"))   # ile przed startem/końcem powiadomić
NOTIFY_INTERVAL = int(os.getenv("NOTIFY_INTERVAL", "60"))
NOTIFY_ENABLED = os.getenv("NOTIFY_ENABLED", "true").lower() in {"1", "true", "yes"}

# Energia / CO2: współczynnik emisyjności sieci (kg CO2 na kWh).
CO2_KG_PER_KWH = float(os.getenv("CO2_KG_PER_KWH", "0.4"))

# Automatyczne czyszczenie nieużywanych obrazów oraz limit wersji (tagów) na repo.
IMAGE_CLEANUP_ENABLED = os.getenv("IMAGE_CLEANUP_ENABLED", "true").lower() in {"1", "true", "yes"}
IMAGE_CLEANUP_INTERVAL = int(os.getenv("IMAGE_CLEANUP_INTERVAL", "3600"))
IMAGE_MAX_VERSIONS_PER_REPO = int(os.getenv("IMAGE_MAX_VERSIONS_PER_REPO", "5"))
IMAGE_UNUSED_TTL_SECONDS = int(os.getenv("IMAGE_UNUSED_TTL_SECONDS", str(7 * 24 * 3600)))  # nieużywane: 7 dni

# Kolejka zadań: pętla, która uruchamia zadania w kolejności priorytetu/fair-share.
QUEUE_ENABLED = os.getenv("QUEUE_ENABLED", "true").lower() in {"1", "true", "yes"}
QUEUE_INTERVAL = int(os.getenv("QUEUE_INTERVAL", "15"))

# Ślad audytowy (kto/kiedy/co): każde żądanie ZMIENIAJĄCE stan trafia do tabeli audit_log,
# którą czyta administrator przez GET /audit-log. Logi kontenera nie wystarczają: pod API
# restartuje się (a wtedy `kubectl logs` traci historię) i nikt poza operatorem klastra ich
# nie widzi, więc "kto skasował to konto" było nieodtwarzalne z samej aplikacji.
AUDIT_ENABLED = os.getenv("AUDIT_ENABLED", "true").lower() in {"1", "true", "yes"}
# Odczyty (GET/HEAD/OPTIONS) są pomijane — to setki żądań z odświeżania konsoli na minutę,
# a nie działania. AUDIT_LOG_READS=true włącza je, gdy potrzebny jest pełny ślad dostępu.
AUDIT_LOG_READS = os.getenv("AUDIT_LOG_READS", "false").lower() in {"1", "true", "yes"}
# Retencja wpisów (0 = trzymaj bez limitu). Czyszczone przez reaper (cleanup_loop).
AUDIT_TTL_SECONDS = int(os.getenv("AUDIT_TTL_SECONDS", str(365 * 24 * 3600)))
# Górny limit długości pola detail, aby jeden wpis nie wciągnął całego dużego żądania.
AUDIT_DETAIL_MAX_CHARS = int(os.getenv("AUDIT_DETAIL_MAX_CHARS", "4000"))
# Ścieżki bez wartości audytowej (sondy życia/metryki) — nigdy nie zapisujemy.
AUDIT_SKIP_PATHS = {"/health", "/metrics"}

# Testowe czyszczenie danych: zezwól na POST /admin/purge-data (usuwanie mlm-live-* z wszystkich tabel).
MLM_ENABLE_TEST_PURGE = os.getenv("MLM_ENABLE_TEST_PURGE", "false").lower() in {"1", "true", "yes"}

# Wyniki zadań: zadania piszą do RESULTS_DIR, zamontowanego z trwałego PVC użytkownika (scratch),
# dzięki czemu wyniki przeżywają usunięcie poda i można je pobrać przez API.
RESULTS_DIR = os.getenv("RESULTS_DIR", "/results")
RESULTS_PVC_SUFFIX = os.getenv("RESULTS_PVC_SUFFIX", "scratch")     # PVC: <user>-scratch
RESULTS_READER_IMAGE = os.getenv("RESULTS_READER_IMAGE", "busybox:latest")
# Total deadline for deleting, starting, and connecting to the results-helper Pod.
# Keep it below the client timeout so failures can return a diagnostic HTTP 503 response.
RESULTS_HELPER_BUDGET_SECONDS = int(os.getenv("RESULTS_HELPER_BUDGET_SECONDS", "40"))
# Ile razy (co 2s) czekamy aż results-helper będzie gotowy — ograniczone też przez budżet powyżej.
RESULTS_HELPER_READY_TRIES = int(os.getenv("RESULTS_HELPER_READY_TRIES", "20"))
# Ile razy ponawiamy exec do results-helpera przy przejściowym błędzie połączenia/streamu.
RESULTS_HELPER_EXEC_RETRIES = int(os.getenv("RESULTS_HELPER_EXEC_RETRIES", "3"))
# Timeout pojedynczego odczytu ze strumienia exec. Trzy próby × (ten timeout + 1s backoff)
# muszą zmieścić się w pozostałej części budżetu żądania.
RESULTS_HELPER_EXEC_TIMEOUT = int(os.getenv("RESULTS_HELPER_EXEC_TIMEOUT", "5"))
MAX_RESULT_DOWNLOAD_MB = int(os.getenv("MAX_RESULT_DOWNLOAD_MB", "1024"))
RESULT_TTL_SECONDS = int(os.getenv("RESULT_TTL_SECONDS", str(7 * 24 * 3600)))  # wyniki: domyślnie 7 dni
LOCAL_DEV_DELETED_RESULTS = set()

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# ---------- Modele bazy danych ----------
class UserDB(Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    username = Column(String, unique=True, index=True)
    role = Column(String)
    hashed_password = Column(String)
    quota_cpu = Column(String)
    quota_memory = Column(String)
    quota_gpu = Column(Integer)
    quota_vram_gb = Column(Integer)
    disk_home_gb = Column(Integer)
    disk_scratch_gb = Column(Integer)
    disk_project_gb = Column(Integer)
    email = Column(String, nullable=True)          # powiadomienia e-mail
    slack_id = Column(String, nullable=True)        # opcjonalny identyfikator Slack
    team = Column(String, nullable=True, index=True)  # zespół (analiza per zespół, shared storage)
    priority = Column(Integer, default=0)           # bazowy priorytet użytkownika (fair-share)
    # Limity PER TYP GPU (JSON {typ: liczba}/{typ: GB}). Klucz "default" = fallback.
    # 1xH100 != 1x1050Ti, więc liczymy/limit per model karty.
    gpu_quota_by_type = Column(Text, nullable=True)   # np. {"NVIDIA-H100":2,"default":8}
    vram_quota_by_type = Column(Text, nullable=True)  # np. {"NVIDIA-H100":80,"default":16}

class UserPreferences(Base):
    __tablename__ = "user_preferences"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, unique=True, index=True)
    reminder_lead_time_minutes = Column(Integer, default=5)  # ile minut przed startem/końcem wysłać powiadomienie

class GPUMetric(Base):
    __tablename__ = "gpu_metrics"
    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    user = Column(String, index=True)
    gpu_uuid = Column(String)
    utilization = Column(Float)
    memory_used = Column(Float)
    temperature = Column(Float)
    power = Column(Float)

class Reservation(Base):
    __tablename__ = "reservations"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, index=True)
    gpu_uuid = Column(String, index=True)
    start_time = Column(DateTime)
    end_time = Column(DateTime)
    status = Column(String, default="active")
    priority = Column(Integer, default=0)               # priorytet rezerwacji (wyższy wygrywa konflikty preempcji)
    notified_start = Column(Boolean, default=False)     # czy wysłano powiadomienie o zbliżającym się starcie
    notified_end = Column(Boolean, default=False)       # czy wysłano powiadomienie o zbliżającym się końcu
    gpu_partition = Column(String, nullable=True)       # profil MIG slice (np. "1g.5gb"); puste = cała karta / time-slice
    slice_index = Column(Integer, nullable=True)        # który egzemplarz slice na tej karcie (0..N-1)

class TaskDB(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    user = Column(String, index=True)
    image = Column(String)
    command = Column(Text)
    resources = Column(Text)
    gpu_uuid = Column(String, nullable=True)
    time_limit_seconds = Column(Integer, nullable=True)
    vram_limit_gb = Column(Integer, nullable=True)
    project = Column(String, nullable=True)
    gpu_partition = Column(String, nullable=True)
    status = Column(String, default="submitted")
    created_at = Column(DateTime, default=datetime.utcnow)

class JobDB(Base):
    __tablename__ = "jobs"
    id = Column(Integer, primary_key=True, index=True)
    reservation_id = Column(Integer, index=True)
    task_name = Column(String, nullable=True)
    user_id = Column(String, index=True)
    username = Column(String, index=True)
    gpu_uuid = Column(String, index=True)
    start_time = Column(DateTime)
    end_time = Column(DateTime)
    image = Column(String)
    command = Column(Text)
    resources = Column(Text)
    time_limit_seconds = Column(Integer)
    vram_limit_gb = Column(Integer, nullable=True)
    project = Column(String, nullable=True)
    gpu_partition = Column(String, nullable=True)
    priority = Column(Integer, default=0)
    status = Column(String, default="scheduled")
    # Nazwa nadana przez użytkownika w UI. Frontend WYSYŁA display_name i etykietuje nim
    # zadania (console-shell), ale backend jej nie miał, więc nazwa była po cichu gubiona
    # i lista pokazywała tylko "MLManage job <id>".
    display_name = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class ImageDB(Base):
    __tablename__ = "images"
    id = Column(Integer, primary_key=True, index=True)
    user = Column(String, index=True)
    name = Column(String, index=True)       # nazwa nadana przez użytkownika, np. "benchmark"
    tag = Column(String, default="latest")
    repository = Column(String)             # pełne repo w rejestrze, np. "alice/benchmark"
    pull_ref = Column(String)               # referencja do użycia w zadaniu (localhost:5000/alice/benchmark:latest)
    size_bytes = Column(Integer, nullable=True)
    visibility = Column(String, default="user", index=True)  # user | project | group | everyone
    project = Column(String, nullable=True, index=True)
    group = Column(String, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class QueuedJobDB(Base):
    """Kolejka zadań: priorytety, fair-share, zależności, multi-GPU/multi-node (gang), backfill."""
    __tablename__ = "queued_jobs"
    id = Column(Integer, primary_key=True, index=True)
    user = Column(String, index=True)
    team = Column(String, nullable=True, index=True)
    image = Column(String)
    command = Column(Text)
    resources = Column(Text)                      # JSON resources (limity per pod)
    gpu_uuid = Column(String, nullable=True)
    gpus = Column(Integer, default=1)             # GPU na pod
    replicas = Column(Integer, default=1)         # liczba podów (multi-node / gang scheduling)
    gang = Column(Boolean, default=False)         # wszystko-albo-nic (gang scheduling)
    mpi = Column(Boolean, default=False)          # uruchom jako zadanie MPI (launcher + workers)
    vram_limit_gb = Column(Integer, nullable=True)
    project = Column(String, nullable=True)
    priority = Column(Integer, default=0)         # priorytet zadania
    time_limit_seconds = Column(Integer, nullable=True)
    depends_on = Column(Text, nullable=True)      # JSON: lista id zadań, od których to zależy
    status = Column(String, default="queued", index=True)  # queued|running|completed|failed|cancelled|waiting_deps
    task_names = Column(Text, nullable=True)      # JSON: utworzone nazwy zadań (per replika)
    display_name = Column(String, nullable=True)  # nazwa z UI (patrz JobDB.display_name)
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

class TaskOutcome(Base):
    """Dlaczego pod zadania się zakończył — zapisane PRZED usunięciem poda.

    Kubernetes nie przechowuje nic po usunięciu poda: ani przyczyny, ani logów.
    Zadanie, którego kontener nie wystartował (np. `command` w jednym elemencie:
    exec "sleep 1000" -> executable file not found) nie produkuje żadnych logów,
    a po sprzątnięciu poda `/tasks/{name}/logs` zwracał tylko 404. Ten rekord jest
    jedynym śladem awarii, więc reaper i pętla kolejki zapisują go zawsze.
    """
    __tablename__ = "task_outcomes"
    id = Column(Integer, primary_key=True, index=True)
    task_name = Column(String, unique=True, index=True)
    user = Column(String, index=True)
    phase = Column(String)                        # Succeeded | Failed | Pending (unschedulable)
    reason = Column(String, nullable=True)        # np. StartError, Error, OOMKilled, Evicted
    message = Column(Text, nullable=True)         # komunikat runtime/schedulera
    exit_code = Column(Integer, nullable=True)
    logs = Column(Text, nullable=True)            # ostatnie linie logów zebrane przed usunięciem
    recorded_at = Column(DateTime, default=datetime.utcnow)

class GPUPartitionDB(Base):
    """Pożądany stan partycjonowania pojedynczej karty GPU (per-GPU, hardware-agnostyczny)."""
    __tablename__ = "gpu_partitions"
    id = Column(Integer, primary_key=True, index=True)
    node = Column(String, index=True)              # węzeł, na którym jest karta
    gpu_uuid = Column(String, unique=True, index=True)
    product = Column(String, nullable=True)         # np. "NVIDIA A100..." / "NVIDIA A1000"
    mode = Column(String, default="full")           # full | timeslice | mps | mig
    replicas = Column(Integer, default=1)           # dla timeslice/mps: liczba części
    mig_profiles = Column(Text, nullable=True)      # dla mig: JSON np. {"1g.5gb":4,"2g.10gb":1}
    applied = Column(Boolean, default=False)        # czy zastosowano na klastrze
    updated_at = Column(DateTime, default=datetime.utcnow)

class GroupDB(Base):
    """Grupa użytkowników z łącznym limitem GPU (egzekwowanym przez API)."""
    __tablename__ = "groups"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    total_gpus = Column(Integer, default=0)         # łączny limit GPU (fallback "default")
    total_disk_gb = Column(Integer, default=0)
    gpu_quota_by_type = Column(Text, nullable=True) # limity GPU per typ karty (JSON {typ: liczba})
    created_at = Column(DateTime, default=datetime.utcnow)

class ProjectDB(Base):
    """Projekt z limitem GPU (egzekwowanym przez API). Zadania wskazują project po nazwie."""
    __tablename__ = "projects"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    owner = Column(String, nullable=True)
    total_gpus = Column(Integer, default=0)         # łączny limit GPU (fallback "default")
    shared_storage_gb = Column(Integer, default=0)
    gpu_quota_by_type = Column(Text, nullable=True) # limity GPU per typ karty (JSON {typ: liczba})
    # Project membership is stored as a JSON list because users can belong to multiple projects.
    members = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class AuditLogDB(Base):
    """Ślad audytowy: kto, kiedy i co zrobił przez API — do wglądu dla administratora.

    Jeden wiersz na żądanie zmieniające stan (POST/PUT/PATCH/DELETE) oraz na każdą próbę
    logowania, także nieudaną. `detail` to zredagowany JSON (hasła/tokeny zastąpione ***),
    uzupełniany przez same endpointy przez audit_note().
    """
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True, index=True)
    at = Column(DateTime, default=datetime.utcnow, index=True)
    actor = Column(String, index=True)              # kto — nazwa użytkownika lub "anonymous"
    actor_role = Column(String, nullable=True)      # rola w chwili zapisu (admin/poweruser/user...)
    action = Column(String, index=True)             # co — np. "users.delete", "auth.login"
    target = Column(String, nullable=True, index=True)  # na czym — parametry ścieżki, np. nazwa konta
    method = Column(String)
    path = Column(String)                           # ścieżka z konkretnymi identyfikatorami
    status_code = Column(Integer, index=True)
    outcome = Column(String, index=True)            # success | denied | rejected | error
    detail = Column(Text, nullable=True)            # JSON: parametry zapytania + notatki endpointu
    client_ip = Column(String, nullable=True)
    user_agent = Column(String, nullable=True)

class DismissedAlertDB(Base):
    """Tabela do śledzenia potwierdzonych alertów przez użytkownika.

    Przechowuje informację o alertach, które użytkownik potwierdzył (dismissed),
    aby uniknąć ciągłych powiadomień o tych samych alertach. Usuwamy wpisy starsze
    niż 24 godziny.
    """
    __tablename__ = "dismissed_alerts"
    id = Column(String, primary_key=True, index=True)  # alert_name-gpu_uuid
    alert_name = Column(String, index=True)
    gpu_uuid = Column(String, nullable=True, index=True)
    dismissed_at = Column(DateTime, default=datetime.utcnow, index=True)
    dismissed_by = Column(String, index=True)

Base.metadata.create_all(bind=engine)

def run_lightweight_migrations():
    """Dodaje brakujące kolumny do istniejących tabel (create_all nie modyfikuje istniejących).

    Idempotentne (ADD COLUMN IF NOT EXISTS) — działa na świeżej i istniejącej bazie.
    """
    sqlite_columns = {
        "users": [
            ("email", "VARCHAR"), ("slack_id", "VARCHAR"), ("team", "VARCHAR"),
            ("priority", "INTEGER DEFAULT 0"), ("gpu_quota_by_type", "TEXT"), ("vram_quota_by_type", "TEXT"),
        ],
        "reservations": [
            ("priority", "INTEGER DEFAULT 0"), ("notified_start", "BOOLEAN DEFAULT FALSE"),
            ("notified_end", "BOOLEAN DEFAULT FALSE"), ("gpu_partition", "VARCHAR"), ("slice_index", "INTEGER"),
        ],
        "groups": [("gpu_quota_by_type", "TEXT")],
        "projects": [("gpu_quota_by_type", "TEXT"), ("members", "TEXT")],
        "images": [("visibility", "VARCHAR DEFAULT 'user'"), ("project", "VARCHAR"), ("group", "VARCHAR")],
        "tasks": [("vram_limit_gb", "INTEGER"), ("project", "VARCHAR"), ("gpu_partition", "VARCHAR")],
        "jobs": [
            ("vram_limit_gb", "INTEGER"), ("project", "VARCHAR"),
            ("gpu_partition", "VARCHAR"), ("priority", "INTEGER DEFAULT 0"),
            ("display_name", "VARCHAR"),
        ],
        "queued_jobs": [("display_name", "VARCHAR")],
    }
    statements = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS slack_id VARCHAR",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS team VARCHAR",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS priority INTEGER DEFAULT 0",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS priority INTEGER DEFAULT 0",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS notified_start BOOLEAN DEFAULT FALSE",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS notified_end BOOLEAN DEFAULT FALSE",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS gpu_partition VARCHAR",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS slice_index INTEGER",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS gpu_quota_by_type TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS vram_quota_by_type TEXT",
        "ALTER TABLE groups ADD COLUMN IF NOT EXISTS gpu_quota_by_type TEXT",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS gpu_quota_by_type TEXT",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS members TEXT",
        "ALTER TABLE images ADD COLUMN IF NOT EXISTS visibility VARCHAR DEFAULT 'user'",
        "ALTER TABLE images ADD COLUMN IF NOT EXISTS project VARCHAR",
        "ALTER TABLE images ADD COLUMN IF NOT EXISTS \"group\" VARCHAR",
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS vram_limit_gb INTEGER",
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS project VARCHAR",
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS gpu_partition VARCHAR",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS vram_limit_gb INTEGER",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS project VARCHAR",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS gpu_partition VARCHAR",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS display_name VARCHAR",
        "ALTER TABLE queued_jobs ADD COLUMN IF NOT EXISTS display_name VARCHAR",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS priority INTEGER DEFAULT 0",
    ]
    try:
        with engine.begin() as conn:
            if DATABASE_URL.startswith("sqlite"):
                for table, columns in sqlite_columns.items():
                    existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()}
                    for name, typ in columns:
                        if name not in existing:
                            column_sql = f'"{name}"' if name == "group" else name
                            conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column_sql} {typ}")
            else:
                for stmt in statements:
                    conn.exec_driver_sql(stmt)
        logger.info("Lightweight migrations applied")
    except Exception as exc:
        logger.error("Migration error: %s", exc)

run_lightweight_migrations()

# ---------- Pydantic schemas ----------
USERNAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$"


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=63, pattern=USERNAME_PATTERN)
    password: str = Field(min_length=1, max_length=1024)


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=63, pattern=USERNAME_PATTERN)
    password: str = Field(min_length=1, max_length=1024)
    role: Literal["admin", "poweruser", "user", "readonly"] = "user"
    quota_cpu: str = "2"
    quota_memory: str = "4Gi"
    quota_gpu: int = 1
    quota_vram_gb: int = 4

class ReservationCreate(BaseModel):
    gpu_uuid: str
    start_time: datetime
    end_time: datetime
    priority: int = 0
    gpu_partition: Optional[str] = None   # rezerwuj konkretny slice MIG (np. "1g.5gb") zamiast całej karty

class TaskCreate(BaseModel):
    image: str
    command: List[str] = Field(default_factory=list)
    resources: Optional[dict] = None
    time_limit_seconds: Optional[int] = None
    gpu_uuid: Optional[str] = None
    vram_limit_gb: Optional[int] = None
    project: Optional[str] = None
    gpu_type: Optional[str] = None        # model karty (np. "NVIDIA-H100"); auto-wykryty z gpu_uuid jeśli pominięty
    gpu_partition: Optional[str] = None   # profil MIG, np. "1g.5gb" -> żądanie nvidia.com/mig-1g.5gb

class JobCreate(BaseModel):
    gpu_uuid: str
    start_time: datetime
    end_time: datetime
    image: str
    command: List[str] = Field(default_factory=list)
    resources: Optional[dict] = None
    vram_limit_gb: Optional[int] = None
    project: Optional[str] = None
    gpu_partition: Optional[str] = None
    priority: int = 0
    display_name: Optional[str] = None   # nazwa z UI, pokazywana na liście zadań

class ReservationRenew(BaseModel):
    end_time: datetime

class CleanupSettings(BaseModel):
    cleanup_interval_seconds: Optional[int] = None
    standalone_task_ttl_seconds: Optional[int] = None
    result_ttl_seconds: Optional[int] = None
    results_helper_idle_ttl_seconds: Optional[int] = None

class PartitionRequest(BaseModel):
    mode: str                                  # full | timeslice | mps | mig
    replicas: Optional[int] = None             # dla timeslice/mps
    mig_profiles: Optional[dict] = None        # dla mig, np. {"1g.5gb":4}

class GroupCreate(BaseModel):
    name: str
    total_gpus: int = 0
    total_disk_gb: int = 0
    gpu_quota_by_type: Optional[dict] = None   # {typ: liczba}, np. {"NVIDIA-H100":2,"default":8}
    members: Optional[List[str]] = None        # nazwy użytkowników przypisanych do grupy (users.team)

class ProjectCreate(BaseModel):
    name: str
    owner: Optional[str] = None
    total_gpus: int = 0
    shared_storage_gb: int = 0
    gpu_quota_by_type: Optional[dict] = None
    members: Optional[List[str]] = None        # utrwalane w projects.members i egzekwowane (ensure_project_access)

class QueueSubmit(BaseModel):
    image: str
    command: List[str] = Field(default_factory=list)
    resources: Optional[dict] = None
    gpu_uuid: Optional[str] = None
    gpus: int = 1                      # GPU na pod
    replicas: int = 1                  # liczba podów (multi-node)
    gang: bool = False                 # gang scheduling (wszystko albo nic)
    mpi: bool = False                  # zadanie MPI (launcher + workers)
    vram_limit_gb: Optional[int] = None
    project: Optional[str] = None
    priority: int = 0
    time_limit_seconds: Optional[int] = None
    depends_on: List[int] = Field(default_factory=list)  # queued-job ids this entry depends on
    display_name: Optional[str] = None   # nazwa z UI, pokazywana na liście zadań

class UserUpdate(BaseModel):
    email: Optional[str] = None
    slack_id: Optional[str] = None
    team: Optional[str] = None
    priority: Optional[int] = None
    gpu_quota_by_type: Optional[dict] = None    # limity GPU per typ karty
    vram_quota_by_type: Optional[dict] = None   # limity VRAM (GB) per typ karty

class ImageUpdate(BaseModel):
    visibility: Optional[str] = None
    project: Optional[str] = None
    group: Optional[str] = None

class PurgeDataRequest(BaseModel):
    confirmation: str = Field(..., description="Must be exactly 'purge-all-data' to confirm the purge")

class ReservationCreatePriority(BaseModel):
    gpu_uuid: str
    start_time: datetime
    end_time: datetime
    priority: int = 0

def serialize_user(user: UserDB):
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "quota_cpu": user.quota_cpu,
        "quota_memory": user.quota_memory,
        "quota_gpu": user.quota_gpu,
        "quota_vram_gb": user.quota_vram_gb,
        "disk_home_gb": user.disk_home_gb,
        "disk_scratch_gb": user.disk_scratch_gb,
        "disk_project_gb": user.disk_project_gb,
        "email": user.email,
        "slack_id": user.slack_id,
        "team": user.team,
        "priority": user.priority,
        "gpu_quota_by_type": json.loads(user.gpu_quota_by_type) if user.gpu_quota_by_type else None,
        "vram_quota_by_type": json.loads(user.vram_quota_by_type) if user.vram_quota_by_type else None,
    }

def queued_job_failure_reason(q: QueuedJobDB, outcomes=None):
    """Krótkie „dlaczego” dla zadania, które się nie udało — z zapisanych wyników podów."""
    if q.status not in ("failed", "completed"):
        return None
    names = json.loads(q.task_names or "[]")
    if outcomes is None:
        outcomes = load_task_outcomes(names)
    reasons = []
    for name in names:
        outcome = outcomes.get(name)
        if not outcome or outcome.get("phase") == "Succeeded":
            continue
        detail = outcome.get("reason") or outcome.get("phase") or "unknown"
        if outcome.get("exit_code") is not None:
            detail = f"{detail} (exit {outcome['exit_code']})"
        if outcome.get("message"):
            detail = f"{detail}: {outcome['message']}"
        reasons.append(f"{name}: {detail}")
    return "; ".join(reasons) or None

def serialize_queued_job(q: QueuedJobDB, outcomes=None):
    return {
        "id": q.id,
        "user": q.user,
        "team": q.team,
        "image": q.image,
        "command": json.loads(q.command or "[]"),
        "resources": json.loads(q.resources or "{}"),
        "gpu_uuid": q.gpu_uuid,
        "gpus": q.gpus,
        "replicas": q.replicas,
        "gang": q.gang,
        "mpi": q.mpi,
        "vram_limit_gb": q.vram_limit_gb,
        "project": q.project,
        "priority": q.priority,
        "time_limit_seconds": q.time_limit_seconds,
        "depends_on": json.loads(q.depends_on or "[]"),
        "status": q.status,
        "task_names": json.loads(q.task_names or "[]"),
        "failure_reason": queued_job_failure_reason(q, outcomes),
        "display_name": q.display_name,
        "created_at": q.created_at.isoformat() if q.created_at else None,
        "started_at": q.started_at.isoformat() if q.started_at else None,
        "finished_at": q.finished_at.isoformat() if q.finished_at else None,
    }

def serialize_image_record(img: ImageDB):
    return {
        "id": img.id,
        "user": img.user,
        "name": img.name,
        "tag": img.tag,
        "repository": img.repository,
        "pull_ref": img.pull_ref,
        "size_bytes": img.size_bytes,
        "visibility": img.visibility or "user",
        "project": img.project,
        "group": img.group,
        "created_at": img.created_at.isoformat() if img.created_at else None,
    }

def serialize_task_record(task: TaskDB):
    return {
        "name": task.name,
        "user": task.user,
        "image": task.image,
        "command": json.loads(task.command or "[]"),
        "resources": json.loads(task.resources or "{}"),
        "gpu_uuid": task.gpu_uuid,
        "time_limit_seconds": task.time_limit_seconds,
        "vram_limit_gb": task.vram_limit_gb,
        "project": task.project,
        "gpu_partition": task.gpu_partition,
        "status": task.status,
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }

def task_failure_reason(task_name, outcomes=None):
    """Jednolinijkowe „dlaczego” dla zadania, z zapisanego wyniku poda."""
    if not task_name:
        return None
    outcome = (outcomes or {}).get(task_name) if outcomes is not None else get_task_outcome(task_name)
    if not outcome or outcome.get("phase") == "Succeeded":
        return None
    detail = outcome.get("reason") or outcome.get("phase") or "unknown"
    if outcome.get("exit_code") is not None:
        detail = f"{detail} (exit {outcome['exit_code']})"
    if outcome.get("message"):
        detail = f"{detail}: {outcome['message']}"
    return detail

def serialize_job_record(job: JobDB, outcomes=None):
    return {
        "id": job.id,
        "reservation_id": job.reservation_id,
        "task_name": job.task_name,
        "user": job.username,
        "gpu_uuid": job.gpu_uuid,
        "start_time": job.start_time.isoformat() if job.start_time else None,
        "end_time": job.end_time.isoformat() if job.end_time else None,
        "image": job.image,
        "command": json.loads(job.command or "[]"),
        "resources": json.loads(job.resources or "{}"),
        "time_limit_seconds": job.time_limit_seconds,
        "vram_limit_gb": job.vram_limit_gb,
        "project": job.project,
        "gpu_partition": job.gpu_partition,
        "priority": job.priority or 0,
        "status": job.status,
        "failure_reason": task_failure_reason(job.task_name, outcomes),
        "display_name": job.display_name,
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }

# ---------- Autoryzacja ----------
security = HTTPBearer()

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if username is None:
            raise HTTPException(status_code=401, detail="Invalid token")
        db = SessionLocal()
        user = db.query(UserDB).filter(UserDB.username == username).first()
        db.close()
        if user is None:
            raise HTTPException(status_code=401, detail="User not found")
        return user
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

def check_role(allowed_roles: List[str]):
    def dependency(current_user: UserDB = Depends(get_current_user)):
        if current_user.role not in allowed_roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return current_user
    return dependency

def load_kubernetes_config():
    try:
        kubernetes.config.load_incluster_config()
    except Exception:
        kubernetes.config.load_kube_config()


def pvc_storage_class_spec() -> dict:
    """Zwraca fragment specu PVC z klasą storage — albo PUSTY dict.

    Pusty dict = brak pola storageClassName => Kubernetes użyje DOMYŚLNEJ klasy klastra.
    Dzięki temu ten sam kod działa na minikube ("standard") i na k3s ("local-path"),
    bez zaszywania nazwy, która na jednym z nich nie istnieje.
    """
    return {"storageClassName": WORKSPACE_STORAGE_CLASS} if WORKSPACE_STORAGE_CLASS else {}


def warn_if_no_default_storage_class():
    """Diagnostyka startowa: jeśli nie podano klasy i klaster NIE ma domyślnej, PVC nigdy się nie zbindują.

    Sam brak pola storageClassName jest poprawny tylko wtedy, gdy klaster ma klasę oznaczoną
    adnotacją `storageclass.kubernetes.io/is-default-class=true`. Bez niej wolumeny (a więc
    wyniki zadań i baza) zostaną w Pending — lepiej powiedzieć to głośno w logu przy starcie.
    """
    if LOCAL_DEV_MODE:
        return
    try:
        load_kubernetes_config()
        classes = kubernetes.client.StorageV1Api().list_storage_class().items
    except Exception as exc:
        logger.warning("Could not list StorageClasses to verify workspace storage: %s", exc)
        return
    names = [c.metadata.name for c in classes]
    if WORKSPACE_STORAGE_CLASS:
        if WORKSPACE_STORAGE_CLASS not in names:
            logger.error(
                "WORKSPACE_STORAGE_CLASS=%r does not exist in this cluster (available: %s). "
                "Workspace PVCs will stay Pending, which breaks task pods and result download.",
                WORKSPACE_STORAGE_CLASS, ", ".join(names) or "none")
        else:
            logger.info("Workspace PVCs will use StorageClass %r", WORKSPACE_STORAGE_CLASS)
        return
    default = [c.metadata.name for c in classes
               if (c.metadata.annotations or {}).get(
                   "storageclass.kubernetes.io/is-default-class") == "true"]
    if default:
        logger.info("Workspace PVCs will use the cluster default StorageClass %r", default[0])
    else:
        logger.error(
            "No default StorageClass in this cluster (available: %s) and WORKSPACE_STORAGE_CLASS "
            "is unset. Workspace PVCs will stay Pending, which breaks task pods and result "
            "download. Set WORKSPACE_STORAGE_CLASS explicitly.", ", ".join(names) or "none")


def heal_unsatisfiable_workspace_pvcs():
    """Odtwarza PVC przestrzeni roboczych, które wiszą w Pending na NIEISTNIEJĄCEJ klasie storage.

    storageClassName jest NIEZMIENNY. PVC utworzony starszą wersją API (gdzie domyślną klasą
    było zaszyte "standard") nigdy się nie zbinduje na klastrze bez tej klasy, a
    create_user_workspaces() pomija go na zawsze (409 "already exists"). Objawy:
    <user>-scratch w Pending, a pody zadań i results-helper nieschedulowalne z komunikatem
    "pod has unbound immediate PersistentVolumeClaims" -> zadania nie startują, wyników nie
    da się pobrać. Sama zmiana domyślnej klasy naprawia tylko NOWE PVC, więc bez tej migracji
    każdy użytkownik założony starym kodem zostaje trwale zepsuty.

    Bezpieczeństwo: ruszamy WYŁĄCZNIE nasze PVC przestrzeni roboczych (etykieta
    devops.dev/volume-type), które są w Pending i wskazują klasę, której w klastrze NIE MA.
    PVC w Pending nigdy się nie zbindował, więc nie może zawierać danych.
    """
    if LOCAL_DEV_MODE or not HEAL_WORKSPACE_PVCS:
        return
    try:
        load_kubernetes_config()
        v1 = kubernetes.client.CoreV1Api()
        existing = {c.metadata.name for c in kubernetes.client.StorageV1Api().list_storage_class().items}
        claims = v1.list_persistent_volume_claim_for_all_namespaces(
            label_selector="devops.dev/volume-type").items
    except Exception as exc:
        logger.warning("Could not scan workspace PVCs for unsatisfiable StorageClasses: %s", exc)
        return
    for pvc in claims:
        sc = pvc.spec.storage_class_name
        ns, name = pvc.metadata.namespace, pvc.metadata.name
        if pvc.status.phase != "Pending" or not sc or sc in existing:
            continue
        logger.error("Workspace PVC %s/%s is Pending on missing StorageClass %r — recreating it "
                     "so the cluster default applies.", ns, name, sc)
        body = {
            "metadata": {"name": name, "labels": pvc.metadata.labels or {}},
            "spec": {
                "accessModes": pvc.spec.access_modes or ["ReadWriteOnce"],
                # celowo BEZ storageClassName -> Kubernetes użyje klasy domyślnej
                **pvc_storage_class_spec(),
                "resources": {"requests": {
                    "storage": ((pvc.spec.resources.requests or {}).get("storage", "10Gi")
                                if pvc.spec.resources else "10Gi")}},
            },
        }
        try:
            v1.delete_namespaced_persistent_volume_claim(name, ns)
            for _ in range(30):
                try:
                    v1.read_namespaced_persistent_volume_claim(name, ns)
                    time.sleep(1)
                except kubernetes.client.exceptions.ApiException:
                    break
            v1.create_namespaced_persistent_volume_claim(namespace=ns, body=body)
            logger.info("Recreated workspace PVC %s/%s without an explicit StorageClass", ns, name)
        except kubernetes.client.exceptions.ApiException as exc:
            logger.warning("Could not recreate workspace PVC %s/%s: %s", ns, name, exc)


def naive_utc(value: datetime):
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)

def create_user_workspaces(v1, namespace: str, username: str, sizes: dict):
    """Create dedicated home, scratch, and project PVCs in the user's namespace.

    Each PVC is labeled by volume type so the disk controller can classify workspace usage.
    """
    for volume_type, size_gb in sizes.items():
        pvc_name = f"{username}-{volume_type}"
        pvc_body = {
            "metadata": {
                "name": pvc_name,
                "labels": {
                    "devops.dev/user": username,
                    "devops.dev/volume-type": volume_type,
                },
            },
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                **pvc_storage_class_spec(),
                "resources": {"requests": {"storage": f"{size_gb}Gi"}},
            },
        }
        try:
            v1.create_namespaced_persistent_volume_claim(namespace=namespace, body=pvc_body)
            logger.info(f"Created workspace PVC {pvc_name} ({size_gb}Gi) in {namespace}")
        except kubernetes.client.exceptions.ApiException as e:
            if e.status == 409:
                logger.info(f"Workspace PVC {pvc_name} already exists, skipping")
            else:
                logger.warning(f"Could not create workspace PVC {pvc_name}: {e}")

def local_dev_gpus():
    if not LOCAL_DEV_GPUS:
        return None
    try:
        parsed = json.loads(LOCAL_DEV_GPUS)
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError as exc:
        logger.error("Invalid LOCAL_DEV_GPUS JSON: %s", exc)
    return None

# ---------- Powiadomienia (e-mail / Slack) ----------
def send_email(to_addr: str, subject: str, body: str):
    """Wysyła e-mail przez SMTP. No-op, jeśli SMTP nie jest skonfigurowany lub brak adresata."""
    if not SMTP_HOST or not to_addr:
        return False
    try:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = to_addr
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as s:
            try:
                s.starttls()
            except Exception:
                pass
            if SMTP_USER:
                s.login(SMTP_USER, SMTP_PASSWORD)
            s.sendmail(SMTP_FROM, [to_addr], msg.as_string())
        return True
    except Exception as exc:
        logger.warning("Email send failed: %s", exc)
        return False

def send_slack(text: str):
    """Wysyła wiadomość na Slack webhook. No-op, jeśli webhook nie jest ustawiony."""
    if not SLACK_WEBHOOK_URL:
        return False
    try:
        requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=5)
        return True
    except Exception as exc:
        logger.warning("Slack send failed: %s", exc)
        return False

def notify_user(user: "UserDB", subject: str, body: str):
    """Powiadamia użytkownika kanałami, które są dostępne (e-mail i/lub Slack)."""
    sent = False
    if user is not None and getattr(user, "email", None):
        sent = send_email(user.email, subject, body) or sent
    sent = send_slack(f"{subject} — {body}") or sent
    return sent

# ---------- FastAPI app ----------
app = FastAPI(title="DevOps Backend API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health():
    return {"status": "ok"}

# ---------- Ślad audytowy ----------
# Notatki bieżącego żądania. Middleware zakłada pusty słownik PRZED wywołaniem endpointu,
# więc endpoint (który zna już sparsowane dane wejściowe) dopisuje do TEGO SAMEGO obiektu
# przez audit_note(). Sam ContextVar nie „wraca” z zadania potomnego, ale współdzielony
# słownik owszem — dlatego trzymamy tu mutowalny dict, a nie wartość.
_audit_notes: ContextVar[Optional[dict]] = ContextVar("audit_notes", default=None)

# Nazwy pól, których wartości NIGDY nie zapisujemy (hasła, tokeny, klucze).
AUDIT_SECRET_PATTERN = re.compile(r"pass|secret|token|authorization|api[_-]?key|credential", re.I)

# Czytelne nazwy akcji tam, gdzie sama ścieżka nie mówi, co się stało.
AUDIT_ACTION_OVERRIDES = {
    ("POST", "/login"): "auth.login",
    ("POST", "/queue"): "job.submit",
    ("POST", "/jobs"): "job.schedule",
    ("POST", "/tasks"): "task.submit",
    ("POST", "/images"): "image.upload",
}
AUDIT_METHOD_VERBS = {"POST": "create", "PUT": "update", "PATCH": "update", "DELETE": "delete", "GET": "read"}


def audit_note(**fields):
    """Dopisuje szczegóły do wpisu audytowego bieżącego żądania (no-op poza żądaniem).

    Wołane z endpointu, który jako jedyny wie, CO faktycznie zmienił (np. nowa rola konta,
    limit GPU grupy). Middleware nie czyta ciała żądania — nie chcemy buforować w pamięci
    wielogigabajtowych uploadów obrazów ani polegać na szczegółach implementacji ASGI.
    """
    notes = _audit_notes.get()
    if notes is not None:
        notes.update({key: value for key, value in fields.items() if value is not None})


def audit_redact(value):
    """Zwraca kopię struktury z wartościami pól „sekretnych” zamienionymi na ***.

    Wyjątkiem od wzorca są wartości logiczne (i None): nazwa pola bywa zbieżna ze wzorcem,
    choć wartość tajna nie jest — `password_correct=False` to diagnostyka nieudanego
    logowania (obiecana wprost w Documentation/03-api/authentication-and-roles.md), a nie
    hasło. Bez tego wyjątku audyt zapisywał `password_correct: "***"`, tracąc jedyną
    informację, po co ten wpis istnieje. Napisy, liczby ORAZ zagnieżdżone struktury pod
    „sekretnym” kluczem redagujemy nadal w całości — w środku mogą siedzieć sekrety, których
    własne klucze do wzorca nie pasują (np. {"credential": {"pw": "..."}}).
    """
    if isinstance(value, dict):
        return {
            key: ("***" if (AUDIT_SECRET_PATTERN.search(str(key))
                            and not isinstance(item, bool) and item is not None)
                  else audit_redact(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [audit_redact(item) for item in value]
    return value


def audit_action_name(method: str, template: str) -> str:
    """Stabilna nazwa akcji z SZABLONU trasy, np. ("DELETE", "/users/{username}") -> users.delete."""
    override = AUDIT_ACTION_OVERRIDES.get((method, template))
    if override:
        return override
    static = [part for part in template.strip("/").split("/") if part and not part.startswith("{")]
    resource = ".".join(static) or "root"
    return f"{resource}.{AUDIT_METHOD_VERBS.get(method, method.lower())}"


def audit_route_match(method: str, path: str):
    """Dopasowuje ścieżkę do trasy i zwraca (szablon, cel).

    Szablon daje nazwę akcji niezależną od identyfikatorów, a parametry ścieżki — obiekt,
    którego dotyczy operacja (np. "alice" dla /users/alice). Dopasowujemy sami, zamiast
    czytać scope["path_params"] po call_next, żeby nie zależeć od tego, czy router
    zdążył uzupełnić ten sam słownik scope.
    """
    scope = {"type": "http", "method": method, "path": path, "path_params": {}, "root_path": "", "headers": []}
    for route in app.routes:
        try:
            match, child = route.matches(scope)
        except Exception:
            continue
        if match == Match.FULL:
            params = (child or {}).get("path_params") or {}
            return getattr(route, "path", path), "/".join(str(value) for value in params.values())
    return path, ""


def audit_actor_from_token(authorization: Optional[str]) -> Optional[str]:
    """Nazwa użytkownika z nagłówka Bearer — bez zapytania do bazy i bez rzucania wyjątku."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    try:
        payload = jwt.decode(authorization.split(" ", 1)[1].strip(), SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None
    subject = payload.get("sub")
    return subject if isinstance(subject, str) else None


def audit_outcome(status_code: int) -> str:
    """Skrót „jak się skończyło”, żeby dało się filtrować bez znajomości kodów HTTP."""
    if status_code < 400:
        return "success"
    if status_code in (401, 403):
        return "denied"     # brak uprawnień / zły token — najciekawsze wpisy w audycie
    if status_code < 500:
        return "rejected"   # walidacja, limity, konflikt
    return "error"


def write_audit_entry(entry: dict):
    """Zapisuje jeden wiersz audytu. Wołane w wątku roboczym, nigdy nie rzuca do żądania."""
    db = SessionLocal()
    try:
        actor = entry.get("actor") or "anonymous"
        actor_role = None
        if actor != "anonymous":
            actor_role = db.query(UserDB.role).filter(UserDB.username == actor).scalar()
        detail = entry.get("detail") or {}
        detail_text = json.dumps(audit_redact(detail), default=str, ensure_ascii=False) if detail else None
        if detail_text and len(detail_text) > AUDIT_DETAIL_MAX_CHARS:
            detail_text = detail_text[:AUDIT_DETAIL_MAX_CHARS] + "…(truncated)"
        db.add(AuditLogDB(
            actor=actor,
            actor_role=actor_role,
            action=entry.get("action"),
            target=entry.get("target") or None,
            method=entry.get("method"),
            path=entry.get("path"),
            status_code=entry.get("status_code"),
            outcome=audit_outcome(int(entry.get("status_code") or 0)),
            detail=detail_text,
            client_ip=entry.get("client_ip"),
            user_agent=(entry.get("user_agent") or "")[:255] or None,
        ))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def purge_old_audit_entries():
    """Usuwa wpisy starsze niż AUDIT_TTL_SECONDS (0 = bez limitu). Wołane przez reaper."""
    if not AUDIT_ENABLED or AUDIT_TTL_SECONDS <= 0:
        return
    cutoff = datetime.utcnow() - timedelta(seconds=AUDIT_TTL_SECONDS)
    db = SessionLocal()
    try:
        removed = db.query(AuditLogDB).filter(AuditLogDB.at < cutoff).delete(synchronize_session=False)
        db.commit()
        if removed:
            logger.info("Audit log: purged %s entries older than %ss", removed, AUDIT_TTL_SECONDS)
    except Exception as exc:
        db.rollback()
        logger.warning("Audit log purge failed: %s", exc)
    finally:
        db.close()


@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    """Zapisuje ślad audytowy dla żądań zmieniających stan — po poznaniu kodu odpowiedzi.

    Zapis idzie do wątku (asyncio.to_thread), bo sesja SQLAlchemy jest synchroniczna, a
    middleware działa w pętli zdarzeń. Błąd zapisu NIGDY nie psuje odpowiedzi: audyt to
    obserwacja, nie warunek działania API.
    """
    read_only = request.method in {"GET", "HEAD", "OPTIONS"}
    if (not AUDIT_ENABLED
            or request.url.path in AUDIT_SKIP_PATHS
            or (read_only and not AUDIT_LOG_READS)):
        return await call_next(request)

    notes: dict = {}
    context_token = _audit_notes.set(notes)
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        _audit_notes.reset(context_token)
        # CAŁY blok w try: wyjątek rzucony z `finally` zastąpiłby gotową odpowiedź błędem
        # 500, czyli wada w samym audycie psułaby działające API. Audyt tylko obserwuje.
        try:
            template, target = audit_route_match(request.method, request.url.path)
            detail = dict(notes)
            query = dict(request.query_params)
            if query:
                detail.setdefault("query", query)
            entry = {
                "actor": notes.get("actor") or audit_actor_from_token(request.headers.get("authorization")),
                "action": notes.get("action") or audit_action_name(request.method, template),
                "target": notes.get("target") or target,
                "method": request.method,
                "path": request.url.path,
                "status_code": status_code,
                "detail": {key: value for key, value in detail.items() if key not in {"actor", "action", "target"}},
                # Za odwrotnym proxy prawdziwy adres klienta jest w X-Forwarded-For.
                "client_ip": (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
                             or (request.client.host if request.client else None),
                "user_agent": request.headers.get("user-agent"),
            }
            await asyncio.to_thread(write_audit_entry, entry)
        except Exception as exc:
            logger.warning("Audit log write failed for %s %s: %s", request.method, request.url.path, exc)

PASSWORD_HASH_ALGORITHM = "pbkdf2_sha256"
PASSWORD_HASH_ITERATIONS = 600_000
PASSWORD_SALT_BYTES = 16


def hash_password(password: str) -> str:
    """Hash a password for storage using salted PBKDF2-HMAC-SHA256."""
    salt = secrets.token_hex(PASSWORD_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PASSWORD_HASH_ITERATIONS
    ).hex()
    return f"{PASSWORD_HASH_ALGORITHM}${PASSWORD_HASH_ITERATIONS}${salt}${digest}"


def verify_password(password: str, encoded_password: str) -> bool:
    """Verify a password without ever accepting legacy plaintext database values."""
    try:
        algorithm, iterations_text, salt, expected_digest = encoded_password.split("$", 3)
        iterations = int(iterations_text)
        if algorithm != PASSWORD_HASH_ALGORITHM or iterations != PASSWORD_HASH_ITERATIONS:
            return False
        actual_digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations
        ).hex()
    except (AttributeError, TypeError, ValueError):
        return False
    return hmac.compare_digest(actual_digest, expected_digest)


DUMMY_PASSWORD_HASH = hash_password("mlmanage-dummy-password")


@app.post("/login")
def login(credentials: LoginRequest):
    username = credentials.username
    db = SessionLocal()
    try:
        user = db.query(UserDB).filter(UserDB.username == username).first()
    finally:
        db.close()

    user_found = user is not None
    password_correct = verify_password(
        credentials.password, user.hashed_password if user_found else DUMMY_PASSWORD_HASH
    )
    logger.info(
        "Login attempt: username=%s, user_found=%s, password_correct=%s",
        username,
        user_found,
        password_correct,
    )
    # Próba logowania jest jedynym żądaniem bez tokenu, więc audyt nie ma skąd wziąć osoby —
    # podajemy ją tutaj. Zapisujemy też próby NIEUDANE: to najważniejszy wpis w całym audycie.
    # Rozróżnienie "nie ma takiego konta" / "złe hasło" jest widoczne tylko dla administratora
    # w /audit-log; odpowiedź logowania pozostaje jednakowa (401 "Invalid credentials").
    audit_note(actor=username, target=username, user_exists=user_found, password_correct=password_correct)
    if not user_found or not password_correct:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = jwt.encode({"sub": user.username, "exp": datetime.utcnow() + timedelta(hours=24)}, SECRET_KEY)
    return {"access_token": token}

@app.get("/me")
def me(current_user: UserDB = Depends(get_current_user)):
    return serialize_user(current_user)

@app.get("/users", dependencies=[Depends(check_role(["admin"]))])
def list_users():
    db = SessionLocal()
    users = db.query(UserDB).order_by(UserDB.username).all()
    result = [serialize_user(user) for user in users]
    db.close()
    return result

@app.post("/users", dependencies=[Depends(check_role(["admin"]))])
def create_user(user: UserCreate):
    audit_note(target=user.username, role=user.role, quota_gpu=user.quota_gpu,
               quota_vram_gb=user.quota_vram_gb, quota_cpu=user.quota_cpu, quota_memory=user.quota_memory)
    db = SessionLocal()
    existing = db.query(UserDB).filter(UserDB.username == user.username).first()
    if existing:
        raise HTTPException(status_code=400, detail="Username exists")
    new_user = UserDB(
        username=user.username,
        hashed_password=hash_password(user.password),
        role=user.role,
        quota_cpu=user.quota_cpu,
        quota_memory=user.quota_memory,
        quota_gpu=user.quota_gpu,
        quota_vram_gb=user.quota_vram_gb
    )
    # Initialize the user's workspace sizes from deployment defaults.
    new_user.disk_home_gb = DEFAULT_DISK_HOME_GB
    new_user.disk_scratch_gb = DEFAULT_DISK_SCRATCH_GB
    new_user.disk_project_gb = DEFAULT_DISK_PROJECT_GB
    db.add(new_user)
    # Tworzenie namespace dla użytkownika
    try:
        load_kubernetes_config()
        v1 = kubernetes.client.CoreV1Api()
        ns_name = f"user-{user.username}"
        # Etykieta devops.dev/user-namespace=true pozwala kontrolerowi dyskowemu wykrywać namespace.
        v1.create_namespace(body={"metadata": {"name": ns_name, "labels": {
            "devops.dev/user": user.username,
            "devops.dev/user-namespace": "true",
        }}})
        logger.info(f"Created namespace {ns_name}")
        # Provision dedicated home, scratch, and project workspace PVCs.
        create_user_workspaces(v1, ns_name, user.username, {
            "home": DEFAULT_DISK_HOME_GB,
            "scratch": DEFAULT_DISK_SCRATCH_GB,
            "project": DEFAULT_DISK_PROJECT_GB,
        })
    except Exception as e:
        logger.warning(f"Could not create namespace: {e}")
    db.commit()
    db.refresh(new_user)
    result = {"message": f"User {user.username} created", "user": serialize_user(new_user)}
    db.close()
    return result

GPU_SIMULATION_LABEL = "mlmanage.dev/gpu-simulated"
# Prefiks klucza profilu device-plugina, który zapisuje MLManage (`mlm-<node>`). Po nim
# poznajemy, że stan podziału węzła opisują nasze rekordy, a nie konfiguracja z zewnątrz.
MANAGED_PLUGIN_CONFIG_PREFIX = "mlm-"


def is_simulated_gpu_node(node) -> bool:
    labels = node.metadata.labels or {}
    return labels.get(GPU_SIMULATION_LABEL, "false").lower() == "true"


def node_sharing_is_managed(node) -> bool:
    """Czy profil device-plugina na tym węźle pochodzi z MLManage.

    `apply_device_sharing` etykietuje węzeł `nvidia.com/device-plugin.config` kluczem,
    który sam zapisuje (`mlm-<node>`), albo `default` (profil bez sekcji sharing). Kiedy
    ta etykieta jest nasza, PEŁNY stan podziału tego węzła jest w rekordach partycji, więc
    karta bez rekordu jest cała — nie wolno wtedy zgadywać z etykiety węzła.
    """
    cfg = (node.metadata.labels or {}).get("nvidia.com/device-plugin.config", "")
    return cfg == "default" or cfg.startswith(MANAGED_PLUGIN_CONFIG_PREFIX)


def gpu_shares_for_node(node) -> int:
    """Ile udziałów publikuje węzeł domyślnie (1 = całe karty, czyli brak podziału).

    Etykieta `nvidia.com/gpu.replicas` jest per WĘZEŁ: GPU Feature Discovery ustawia ją
    na całym węźle, nawet gdy podzielona jest jedna karta. Użyta jako domyślna liczba
    udziałów każdej karty pokazywała więc podział na kartach, których nikt nie ruszał
    (6 kart × „4 udziały”, choć podzielona była tylko jedna). Dlatego czytamy ją TYLKO dla
    węzłów, których profilu nie ustawiał MLManage (konfiguracja z zewnątrz, jednorodna).
    Dla węzłów zarządzanych przez nas domyślną wartością jest 1 — podział opisują rekordy.
    """
    if node_sharing_is_managed(node):
        return 1
    labels = node.metadata.labels or {}
    try:
        return max(1, int(labels.get("nvidia.com/gpu.replicas", 1) or 1))
    except (TypeError, ValueError):
        return 1


def sharing_modes_by_gpu(node_name: Optional[str] = None):
    """{gpu_uuid: "timeslice"|"mps"} tylko dla kart faktycznie współdzielonych.

    Etykieta `nvidia.com/gpu.sharing-strategy` jest per WĘZEŁ: po włączeniu MPS na jednej
    karcie nosi ją cały węzeł, więc pokazywana jako mechanizm każdej karty sugerowała
    współdzielenie tam, gdzie go nie ma. Karta z rekordem opisuje się sama.
    """
    db = SessionLocal()
    try:
        query = db.query(GPUPartitionDB)
        if node_name is not None:
            query = query.filter(GPUPartitionDB.node == node_name)
        return {
            record.gpu_uuid: record.mode
            for record in query.all()
            if record.mode in ("timeslice", "mps") and (record.replicas or 1) > 1
        }
    except Exception:
        return {}
    finally:
        db.close()


def node_sharing_strategy(node):
    """Etykieta `nvidia.com/gpu.sharing-strategy` — tylko dla węzłów spoza MLManage.

    Na węźle, którego profil ustawiamy sami, mechanizm każdej karty wynika z jej rekordu;
    etykieta (per węzeł) mówiłaby „mps” także o kartach, które nie są podzielone.
    """
    if node_sharing_is_managed(node):
        return None
    return (node.metadata.labels or {}).get("nvidia.com/gpu.sharing-strategy") or None


def shares_by_gpu(node_name: Optional[str] = None):
    """{gpu_uuid: liczba udziałów} z rekordów partycji (podział jest per karta).

    Karta w trybie `full` (albo bez rekordu) NIE jest podzielona, więc ma 1 udział.
    """
    db = SessionLocal()
    try:
        query = db.query(GPUPartitionDB)
        if node_name is not None:
            query = query.filter(GPUPartitionDB.node == node_name)
        return {
            record.gpu_uuid: (
                max(1, int(record.replicas or 1))
                if record.mode in ("timeslice", "mps")
                else 1
            )
            for record in query.all()
        }
    except Exception:
        return {}
    finally:
        db.close()


def physical_gpu_product(labels: dict) -> str:
    """Model karty bez sufiksu `-SHARED`, który dokłada GPU Operator przy time-slicingu.

    Karta pozostaje tym samym modelem, kiedy jest współdzielona. Bez tego włączenie
    współdzielenia zmieniało `NVIDIA-RTX-A5000` na `NVIDIA-RTX-A5000-SHARED`, więc limity
    per model (kluczowane modelem) przestawały pasować i cicho przestawały obowiązywać.
    """
    product = labels.get("nvidia.com/gpu.product", "unknown") or "unknown"
    return product[: -len("-SHARED")] if product.endswith("-SHARED") else product


def gpu_memory_gb_for_node(labels: dict):
    """Pamięć karty w GB z etykiety `nvidia.com/gpu.memory` (MiB); None, gdy brak."""
    try:
        mib = int(labels.get("nvidia.com/gpu.memory", 0) or 0)
    except (TypeError, ValueError):
        return None
    return round(mib / 1024) if mib > 0 else None


def gpu_count_for_node(node) -> int:
    """Liczba FIZYCZNYCH kart na węźle, a nie jednostek do zaplanowania.

    Przy time-slicingu/MPS device plugin publikuje `pojemność = karty × udziały`.
    Listowanie pojemności jako osobnych kart wymyślało sprzęt, którego nie ma: włączenie
    2 udziałów na 6 kartach dawało 12 pozycji w inwentarzu, a powrót do `full` zostawiał
    rekordy i rezerwacje wskazujące na karty, które zniknęły.
    """
    labels = node.metadata.labels or {}
    if is_simulated_gpu_node(node):
        return int(labels.get("nvidia.com/gpu.count", 0) or 0)
    try:
        physical = int(labels.get("nvidia.com/gpu.count", 0) or 0)
    except (TypeError, ValueError):
        physical = 0
    if physical:
        return physical
    capacity = int((node.status.capacity or {}).get("nvidia.com/gpu", 0) or 0)
    if node_sharing_is_managed(node):
        # Podział jest per karta, więc dzielenie pojemności przez jedną liczbę udziałów
        # nie ma tu sensu: każdy rekord dokłada `udziały - 1` jednostek ponad swoją kartę.
        extra = sum(max(1, s) - 1 for s in shares_by_gpu(node.metadata.name).values())
        return max(0, capacity - extra)
    return capacity // gpu_shares_for_node(node)


@app.get("/gpu/list")
def list_gpus():
    try:
        load_kubernetes_config()
        v1 = kubernetes.client.CoreV1Api()
        nodes = v1.list_node(label_selector="nvidia.com/gpu.present=true")
        gpus = []
        per_gpu_shares = shares_by_gpu()
        per_gpu_modes = sharing_modes_by_gpu()
        for node in nodes.items:
            labels = node.metadata.labels or {}
            gpu_product = physical_gpu_product(labels)
            simulated = is_simulated_gpu_node(node)
            node_shares = gpu_shares_for_node(node)
            node_strategy = node_sharing_strategy(node)
            memory_gb = gpu_memory_gb_for_node(labels)
            for i in range(gpu_count_for_node(node)):
                gpu_uuid = labels.get(f"nvidia.com/gpu.uuid.{i}", f"gpu-{node.metadata.name}-{i}")
                gpus.append({
                    "uuid": gpu_uuid,
                    "product": gpu_product,
                    "node": node.metadata.name,
                    "simulated": simulated,
                    # Ile jednostek publikuje TA karta i jakim mechanizmem — inaczej UI nie
                    # ma jak powiedzieć, że karta jest już podzielona. Podział jest per
                    # karta, więc rekord tej karty ma pierwszeństwo nad etykietą węzła.
                    "shares": per_gpu_shares.get(gpu_uuid, node_shares),
                    "sharing_strategy": per_gpu_modes.get(gpu_uuid, node_strategy),
                    **({"memory_gb": memory_gb} if memory_gb else {}),
                })
        return {"gpus": gpus}
    except Exception as e:
        configured_gpus = local_dev_gpus()
        if configured_gpus is not None:
            logger.warning("Using LOCAL_DEV_GPUS because Kubernetes GPU discovery failed: %s", e)
            return {"gpus": configured_gpus}
        logger.error(f"GPU list error: {e}")
        return {"gpus": [], "error": str(e)}

# ---------- Partycjonowanie GPU (hardware-agnostyczne) ----------
def detect_gpu_capabilities():
    """Wykrywa karty i ich możliwości partycjonowania z etykiet węzłów (GPU Operator/NFD).

    Zwraca listę: {node, gpu_uuid, product, mig_capable, mig_strategy, current...}.
    Nie zakłada konkretnego sprzętu — odczytuje etykiety i wnioskuje możliwości.
    """
    load_kubernetes_config()
    v1 = kubernetes.client.CoreV1Api()
    out = []
    # Preferujemy węzły oznaczone przez NFD; w razie braku etykiet bierzemy węzły z
    # pojemnością nvidia.com/gpu (działa też na minimalnych klastrach bez pełnego NFD).
    nodes = v1.list_node(label_selector="nvidia.com/gpu.present=true").items
    if not nodes:
        nodes = [n for n in v1.list_node().items
                 if int((n.status.capacity or {}).get("nvidia.com/gpu", 0) or 0) > 0]
    per_gpu_shares = shares_by_gpu()
    per_gpu_modes = sharing_modes_by_gpu()
    for node in nodes:
        lbl = node.metadata.labels or {}
        product = physical_gpu_product(lbl)
        count = gpu_count_for_node(node)
        node_shares = gpu_shares_for_node(node)
        node_strategy = node_sharing_strategy(node)
        memory_gb = gpu_memory_gb_for_node(lbl)
        simulated = is_simulated_gpu_node(node)
        # MIG capability: GPU Operator/NFD oznacza karty zdolne do MIG.
        mig_capable = lbl.get("nvidia.com/mig.capable", "false") == "true"
        mig_strategy = lbl.get("nvidia.com/mig.strategy", "none")
        for i in range(count):
            uuid = lbl.get(f"nvidia.com/gpu.uuid.{i}", f"gpu-{node.metadata.name}-{i}")
            out.append({
                "node": node.metadata.name,
                "gpu_uuid": uuid,
                "product": product,
                "mig_capable": mig_capable,
                "mig_strategy": mig_strategy,
                "simulated": simulated,
                # Podział jest per karta; etykieta węzła to tylko fallback dla konfiguracji
                # ustawionych poza MLManage (jednorodnych dla całego węzła).
                "shares": per_gpu_shares.get(uuid, node_shares),
                "sharing_strategy": per_gpu_modes.get(uuid, node_strategy),
                **({"memory_gb": memory_gb} if memory_gb else {}),
                # Simulated devices are inventory-only and cannot be partitioned or scheduled.
                "supported_modes": [] if simulated else ["full", "timeslice", "mps"] + (["mig"] if mig_capable else []),
            })
    return out

def ensure_schedulable_gpu_uuid(gpu_uuid: Optional[str]):
    """Reject inventory-only simulated devices for real Kubernetes workloads."""
    if LOCAL_DEV_MODE or not gpu_uuid:
        return
    capability = next((item for item in detect_gpu_capabilities() if item["gpu_uuid"] == gpu_uuid), None)
    if capability and capability.get("simulated"):
        raise HTTPException(
            status_code=409,
            detail="Simulated GPUs are inventory-only and cannot be used for workloads or reservations",
        )


def _put_plugin_profile(v1, key: str, yaml_text: str):
    """Wstawia/aktualizuje JEDEN profil w ConfigMapie device-plugina, nie ruszając reszty."""
    try:
        existing = v1.read_namespaced_config_map(TIME_SLICING_CONFIGMAP, GPU_OPERATOR_NS)
        merged = dict(existing.data or {})
        # `default` musi ISTNIEĆ (węzeł bez etykiety trafia właśnie na ten profil), ale jeśli
        # operator go dostroił, to go NIE nadpisujemy — inaczej każdy zapis podziału po cichu
        # cofałby ustawienia całego klastra do „brak sharingu”.
        merged.setdefault("default", "version: v1\n")
        if key != "default":
            merged[key] = yaml_text
        v1.patch_namespaced_config_map(TIME_SLICING_CONFIGMAP, GPU_OPERATOR_NS, {"data": merged})
    except kubernetes.client.exceptions.ApiException as e:
        if e.status != 404:
            raise
        v1.create_namespaced_config_map(GPU_OPERATOR_NS, {
            "metadata": {"name": TIME_SLICING_CONFIGMAP, "namespace": GPU_OPERATOR_NS},
            "data": {"default": "version: v1\n", key: yaml_text},
        })


def gpu_device_index(node_name: str, gpu_uuid: str):
    """Indeks urządzenia, którym device-plugin adresuje tę kartę.

    Najpierw etykiety `nvidia.com/gpu.uuid.<i>`; w ich braku indeks jest sufiksem
    syntetycznego identyfikatora `gpu-<node>-<i>`, którym listuje ta sama funkcja.
    """
    mapped = gpu_index_map(node_name).get(gpu_uuid)
    if mapped is not None:
        return mapped
    prefix = f"gpu-{node_name}-"
    if gpu_uuid.startswith(prefix):
        suffix = gpu_uuid[len(prefix):]
        if suffix.isdigit():
            return int(suffix)
    return None


def apply_device_sharing(node: str, db):
    """Publikuje współdzielenie PER KARTA dla jednego węzła.

    Device-plugin przyjmuje `sharing.<mode>.resources[].devices`, więc podział dotyczy
    wskazanych kart, a nie całego węzła: karty bez rekordu (albo w trybie `full`) nadal
    ogłaszają się jako jedno całe urządzenie. Profil jest budowany z WSZYSTKICH rekordów
    tego węzła, więc podzielenie jednej karty nie zdejmuje podziału z pozostałych.

    Time-slicing i MPS są w device-pluginie wzajemnie wykluczające się w obrębie jednej
    konfiguracji, a konfiguracja jest per węzeł — dwie karty tego samego węzła nie mogą
    więc być podzielone różnymi mechanizmami. To ograniczenie sprzętowe/pluginowe, nie
    decyzja tego API, dlatego zgłaszamy je wyraźnie.
    """
    load_kubernetes_config()
    v1 = kubernetes.client.CoreV1Api()
    shared = {"timeslice": [], "mps": []}
    for record in db.query(GPUPartitionDB).filter(GPUPartitionDB.node == node).all():
        if record.mode not in ("timeslice", "mps") or (record.replicas or 1) < 2:
            continue
        index = gpu_device_index(node, record.gpu_uuid)
        if index is None:
            raise RuntimeError(
                f"cannot resolve a device index for {record.gpu_uuid}; per-card sharing needs one"
            )
        shared[record.mode].append((index, int(record.replicas)))
    if shared["timeslice"] and shared["mps"]:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Node {node} cannot run time sharing and concurrent sharing at the same time: "
                "the device plugin takes one sharing mechanism per node. Rejoin the cards that "
                "use the other mechanism first."
            ),
        )
    mode = "timeslice" if shared["timeslice"] else "mps" if shared["mps"] else None
    if not mode:
        # Nic nie jest już podzielone: węzeł wraca na profil bez sekcji sharing.
        _put_plugin_profile(v1, "default", "version: v1\n")
        v1.patch_node(node, {"metadata": {"labels": {"nvidia.com/device-plugin.config": "default"}}})
        return {"scope": "node", "node": node, "shared_devices": []}
    section = "timeSlicing" if mode == "timeslice" else "mps"
    lines = ["version: v1", "sharing:", f"  {section}:", "    resources:"]
    for index, replicas in sorted(shared[mode]):
        lines += [
            "    - name: nvidia.com/gpu",
            # Indeksy MUSZĄ być łańcuchami. Przy `devices: [2]` plugin nie rozpoznaje
            # listy i po cichu wraca do `devices: "all"`, czyli dzieli wszystkie karty
            # węzła — dokładnie to, czego ten podział ma unikać.
            f'      devices: ["{index}"]',
            f"      replicas: {replicas}",
        ]
    key = f"{MANAGED_PLUGIN_CONFIG_PREFIX}{node}"
    _put_plugin_profile(v1, key, "\n".join(lines) + "\n")
    v1.patch_node(node, {"metadata": {"labels": {"nvidia.com/device-plugin.config": key}}})
    return {
        "scope": "gpu",
        "node": node,
        "mechanism": mode,
        "shared_devices": [{"device": i, "replicas": r} for i, r in sorted(shared[mode])],
    }

def gpu_index_map(node_name: str):
    """Zwraca mapę {gpu_uuid: device_index} dla węzła na podstawie etykiet nvidia.com/gpu.uuid.<i>."""
    load_kubernetes_config()
    v1 = kubernetes.client.CoreV1Api()
    node = v1.read_node(node_name)
    lbl = node.metadata.labels or {}
    # Liczba FIZYCZNYCH kart: pojemność jest przemnożona, kiedy współdzielenie jest włączone.
    count = gpu_count_for_node(node)
    mapping = {}
    for i in range(count):
        u = lbl.get(f"nvidia.com/gpu.uuid.{i}")
        if u:
            mapping[u] = i
    return mapping

def _mig_parted_yaml(per_gpu):
    """Buduje YAML mig-parted z RÓŻNĄ geometrią per-GPU (bez zależności od pyyaml).

    per_gpu: lista (device_index, {profile: count}). Tworzymy jeden wpis 'mlmanage' z osobną
    pozycją mig-configs dla każdej karty (devices: [index]).
    """
    lines = ["version: v1", "mig-configs:", "  mlmanage:"]
    for idx, profiles in per_gpu:
        lines.append(f"  - devices: [{idx}]")
        if profiles:
            lines.append("    mig-enabled: true")
            lines.append("    mig-devices:")
            for prof, cnt in profiles.items():
                lines.append(f"      {prof}: {cnt}")
        else:
            lines.append("    mig-enabled: false")
    return "\n".join(lines) + "\n"

def apply_mig(node: str, desired_uuid: str, desired_profiles: dict):
    """Ustawia RÓŻNĄ geometrię MIG per-GPU na węźle przez własną konfigurację mig-parted.

    Zbiera pożądany stan MIG wszystkich kart węzła (z bazy) + bieżącą zmianę, buduje
    konfigurację z osobnym wpisem device per-GPU i zapisuje ją do ConfigMap mig-parted,
    następnie etykietuje węzeł aby mig-manager ją zastosował.
    """
    load_kubernetes_config()
    v1 = kubernetes.client.CoreV1Api()
    idx_map = gpu_index_map(node)

    # Zbierz docelowy MIG per-GPU: z bazy (inne karty) + bieżąca zmiana.
    db = SessionLocal()
    try:
        node_parts = {p.gpu_uuid: p for p in db.query(GPUPartitionDB).filter(GPUPartitionDB.node == node).all()}
    finally:
        db.close()
    per_gpu = []
    for uuid, idx in sorted(idx_map.items(), key=lambda kv: kv[1]):
        if uuid == desired_uuid:
            profiles = desired_profiles
        else:
            p = node_parts.get(uuid)
            if p and p.mode == "mig" and p.mig_profiles:
                profiles = json.loads(p.mig_profiles)
            else:
                profiles = {}   # karta nie-MIG na tym węźle -> mig wyłączony
        per_gpu.append((idx, profiles))

    cfg_yaml = _mig_parted_yaml(per_gpu)
    body = {"metadata": {"name": MIG_PARTED_CONFIGMAP, "namespace": GPU_OPERATOR_NS},
            "data": {MIG_PARTED_KEY: cfg_yaml}}
    try:
        v1.replace_namespaced_config_map(MIG_PARTED_CONFIGMAP, GPU_OPERATOR_NS, body)
    except kubernetes.client.exceptions.ApiException as e:
        if e.status == 404:
            v1.create_namespaced_config_map(GPU_OPERATOR_NS, body)
        else:
            raise
    # Etykieta wskazuje mig-managerowi nasz wpis konfiguracji.
    v1.patch_node(node, {"metadata": {"labels": {"nvidia.com/mig.config": "mlmanage"}}})
    return {"mig_config": "mlmanage", "configmap": MIG_PARTED_CONFIGMAP, "per_gpu": [
        {"index": i, "profiles": p} for i, p in per_gpu]}

def serialize_partition(p: "GPUPartitionDB"):
    return {
        "gpu_uuid": p.gpu_uuid, "node": p.node, "product": p.product,
        "mode": p.mode, "replicas": p.replicas,
        "mig_profiles": json.loads(p.mig_profiles) if p.mig_profiles else None,
        "applied": p.applied,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }

@app.get("/gpu/capabilities", dependencies=[Depends(check_role(["user", "poweruser", "admin", "readonly"]))])
def gpu_capabilities():
    """Zwraca wykryte karty i co każda wspiera (full/timeslice/mps/mig). Hardware-agnostyczne."""
    try:
        caps = detect_gpu_capabilities()
    except Exception as e:
        return {"gpus": [], "error": str(e)}
    # Dołączamy bieżący pożądany stan z bazy.
    db = SessionLocal()
    parts = {p.gpu_uuid: serialize_partition(p) for p in db.query(GPUPartitionDB).all()}
    db.close()
    for c in caps:
        c["partition"] = parts.get(c["gpu_uuid"])
    return {"gpus": caps}

@app.get("/gpu/partitions", dependencies=[Depends(check_role(["user", "poweruser", "admin", "readonly"]))])
def list_partitions():
    db = SessionLocal()
    res = [serialize_partition(p) for p in db.query(GPUPartitionDB).order_by(GPUPartitionDB.node).all()]
    db.close()
    return {"partitions": res}

@app.post("/gpu/{gpu_uuid}/partition", dependencies=[Depends(check_role(["admin"]))])
def set_partition(gpu_uuid: str, req: "PartitionRequest"):
    """Ustawia/zmienia partycjonowanie konkretnej karty. Admin-only. Waliduje wg możliwości sprzętu."""
    audit_note(mode=req.mode, replicas=req.replicas, mig_profiles=req.mig_profiles)
    caps = {c["gpu_uuid"]: c for c in detect_gpu_capabilities()}
    cap = caps.get(gpu_uuid)
    if not cap:
        raise HTTPException(status_code=404, detail="GPU not found")
    if cap.get("simulated"):
        raise HTTPException(status_code=409, detail="Simulated GPUs are inventory-only and cannot be partitioned")
    mode = req.mode
    if mode not in cap["supported_modes"]:
        raise HTTPException(status_code=400,
            detail=f"Mode '{mode}' not supported by {cap['product']}. Supported: {cap['supported_modes']}")
    db = SessionLocal()
    try:
        p = db.query(GPUPartitionDB).filter(GPUPartitionDB.gpu_uuid == gpu_uuid).first()
        if not p:
            p = GPUPartitionDB(gpu_uuid=gpu_uuid, node=cap["node"], product=cap["product"])
            db.add(p)
        p.node = cap["node"]; p.product = cap["product"]; p.mode = mode; p.updated_at = datetime.utcnow()
        applied_detail = {}
        if mode in ("timeslice", "mps"):
            # `replicas: 1` to karta NIEPODZIELONA, a device plugin i tak pomija taki wpis
            # (patrz `apply_device_sharing`). Przyjęty rekord kłamałby więc o stanie karty:
            # inwentarz pokazywałby tryb współdzielenia, a karta byłaby cała. Trybem
            # „bez podziału” jest `full`, dlatego tutaj wymagamy co najmniej 2 udziałów.
            if not req.replicas or req.replicas < 2:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "replicas (>=2) required for timeslice/mps; use mode 'full' to leave "
                        "the card whole (1 share = not divided)"
                    ),
                )
            p.replicas = req.replicas; p.mig_profiles = None
            # Podział dotyczy TEJ karty: profil pluginu adresuje konkretne urządzenie,
            # więc pozostałe karty węzła zostają całe (albo przy swoim własnym podziale).
            db.flush()
            try:
                applied_detail = apply_device_sharing(cap["node"], db)
                applied_detail["replicas"] = req.replicas
                applied_detail["note"] = (
                    f"{req.replicas} shares published for this card only; the device plugin "
                    "restarts to pick it up (a few seconds)"
                )
                p.applied = True
            except HTTPException:
                db.rollback(); raise
            except Exception as e:
                p.applied = False; applied_detail = {"error": str(e)}
        elif mode == "mig":
            if not req.mig_profiles:
                raise HTTPException(status_code=400, detail="mig_profiles required for mig mode")
            for prof in req.mig_profiles:
                if prof not in MIG_PROFILE_VRAM_GB:
                    raise HTTPException(status_code=400, detail=f"Unknown MIG profile '{prof}'")
            p.mig_profiles = json.dumps(req.mig_profiles); p.replicas = sum(req.mig_profiles.values())
            try:
                cfg = apply_mig(cap["node"], gpu_uuid, req.mig_profiles)
                p.applied = True
                applied_detail = {"scope": "gpu", "note": "per-GPU MIG geometry via custom mig-parted config", **cfg}
            except Exception as e:
                p.applied = False; applied_detail = {"error": str(e)}
        else:  # full
            p.replicas = 1; p.mig_profiles = None
            # Scalenie TEJ karty. Rekord musi być zapisany przed przebudową profilu, żeby
            # ta karta z niego wypadła, a podziały pozostałych kart zostały nietknięte.
            db.flush()
            try:
                applied_detail = apply_device_sharing(cap["node"], db)
                applied_detail["note"] = (
                    "this card republishes as one whole device once the plugin restarts "
                    "(a few seconds); other cards on the node keep their own sharing"
                )
                p.applied = True
            except HTTPException:
                db.rollback(); raise
            except Exception as e:
                p.applied = False
                applied_detail = {"error": str(e)}
        db.commit(); db.refresh(p)
        return {"message": "Partition set", "partition": serialize_partition(p), "applied_detail": applied_detail}
    except HTTPException:
        db.rollback(); raise
    finally:
        db.close()

@app.delete("/gpu/{gpu_uuid}/partition", dependencies=[Depends(check_role(["admin"]))])
def forget_partition(gpu_uuid: str):
    """Usuwa REKORD partycjonowania. Nie zmienia niczego na klastrze.

    Rekordy zostają po kartach, których już nie ma w inwentarzu (np. po powrocie węzła
    z time-slicingu do pełnych kart), i były pokazywane jako sprzęt, który nie istnieje.
    """
    db = SessionLocal()
    try:
        p = db.query(GPUPartitionDB).filter(GPUPartitionDB.gpu_uuid == gpu_uuid).first()
        if not p:
            raise HTTPException(status_code=404, detail="No partition record for this GPU")
        db.delete(p)
        db.commit()
        return {"message": "Partition record removed", "gpu_uuid": gpu_uuid}
    except HTTPException:
        db.rollback(); raise
    finally:
        db.close()

# ---------- Obrazy użytkownika (upload własnego obrazu) ----------
def sanitize_image_name(name: str) -> str:
    """Dozwolone tylko bezpieczne znaki repo Dockera."""
    name = (name or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9]([a-z0-9._-]*[a-z0-9])?", name or ""):
        raise HTTPException(status_code=400, detail="Invalid image name (use lowercase letters, digits, '-', '_', '.')")
    return name

def push_tarball_to_registry(tar_path: str, repository: str, tag: str) -> str:
    """Wypycha obraz z pliku tar (docker save) do rejestru klastra przy użyciu crane.

    Zwraca referencję do pobrania przez pody (przez proxy na węźle).
    """
    if LOCAL_DEV_MODE:
        # Local development mode persists image metadata without requiring a registry or crane.
        logger.info("LOCAL_DEV_MODE image upload accepted without registry push: %s:%s (%s)", repository, tag, tar_path)
        return f"{REGISTRY_PULL_PREFIX}/{repository}:{tag}"
    crane = shutil.which(CRANE_PATH) or CRANE_PATH
    push_ref = f"{REGISTRY_PUSH_HOST}/{repository}:{tag}"
    cmd = [crane, "push", "--insecure", tar_path, push_ref]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        logger.error("crane push failed: %s\n%s", proc.stdout, proc.stderr)
        raise HTTPException(status_code=500, detail=f"Image push failed: {proc.stderr.strip()[:500]}")
    # Pody pobierają obraz przez proxy rejestru na węźle (localhost:5000), nie przez DNS usługi.
    return f"{REGISTRY_PULL_PREFIX}/{repository}:{tag}"

VALID_IMAGE_VISIBILITIES = {"user", "project", "group", "everyone"}

def normalize_image_visibility(visibility: str, project: Optional[str], group: Optional[str], current_user: UserDB):
    """Validate and canonicalize an image's discovery scope.

    Visibility controls which image records appear in `GET /images`; it does not prevent a
    caller from submitting an independently known public registry reference.
    """
    vis = (visibility or "user").lower()
    if vis not in VALID_IMAGE_VISIBILITIES:
        raise HTTPException(status_code=400, detail="visibility must be one of: user, project, group, everyone")
    if vis in {"user", "everyone"}:
        return vis, None, None

    img_project = project.strip() if project else None
    img_group = group.strip() if group else None
    if vis == "project":
        if not img_project:
            raise HTTPException(status_code=400, detail="project is required for project-visible images")
        db = SessionLocal()
        try:
            project_record = db.query(ProjectDB).filter(ProjectDB.name == img_project).first()
        finally:
            db.close()
        if not project_record:
            raise HTTPException(status_code=400, detail="Project not found")
        if current_user.role != "admin" and project_record.owner != current_user.username:
            raise HTTPException(status_code=403, detail="Only the project owner or admin can publish images to this project")
        return vis, img_project, None

    if not img_group:
        img_group = current_user.team
    if not img_group:
        raise HTTPException(status_code=400, detail="group is required for group-visible images when the uploader has no team")
    db = SessionLocal()
    try:
        group_exists = db.query(GroupDB).filter(GroupDB.name == img_group).first() is not None
    finally:
        db.close()
    if not group_exists:
        raise HTTPException(status_code=400, detail="Group not found")
    if current_user.role != "admin" and current_user.team != img_group:
        raise HTTPException(status_code=403, detail="Only group members or admin can publish images to this group")
    return vis, None, img_group

def can_view_image(img: ImageDB, current_user: UserDB, db: Session):
    visibility = img.visibility or "user"
    if current_user.role == "admin" or img.user == current_user.username:
        return True
    if visibility == "everyone":
        return True
    if visibility == "group" and img.group and current_user.team == img.group:
        return True
    if visibility == "project" and img.project:
        project = db.query(ProjectDB).filter(ProjectDB.name == img.project).first()
        return bool(project and project.owner == current_user.username)
    return False

@app.post("/images", dependencies=[Depends(check_role(["user", "poweruser", "admin"]))])
async def upload_image(file: UploadFile = File(...), name: str = Query(...), tag: str = Query("latest"),
                       visibility: str = Query("user"), project: Optional[str] = Query(None), group: Optional[str] = Query(None),
                       current_user: UserDB = Depends(get_current_user)):
    """Upload własnego obrazu jako tar (`docker save img > img.tar`) i wypchnięcie go do rejestru klastra."""
    image_name = sanitize_image_name(name)
    image_tag = sanitize_image_name(tag)
    image_visibility, image_project, image_group = normalize_image_visibility(visibility, project, group, current_user)
    repository = f"{current_user.username}/{image_name}"
    tmp = tempfile.NamedTemporaryFile(prefix="img-", suffix=".tar", delete=False)
    total = 0
    limit = MAX_IMAGE_UPLOAD_MB * 1024 * 1024
    try:
        while True:
            chunk = await file.read(4 * 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise HTTPException(status_code=413, detail=f"Image exceeds {MAX_IMAGE_UPLOAD_MB} MB limit")
            tmp.write(chunk)
        tmp.flush()
        tmp.close()
        pull_ref = push_tarball_to_registry(tmp.name, repository, image_tag)
        db = SessionLocal()
        try:
            existing = db.query(ImageDB).filter(ImageDB.user == current_user.username,
                                                ImageDB.name == image_name, ImageDB.tag == image_tag).first()
            if existing:
                existing.repository = repository
                existing.pull_ref = pull_ref
                existing.size_bytes = total
                existing.visibility = image_visibility
                existing.project = image_project
                existing.group = image_group
                existing.created_at = datetime.utcnow()
                record = existing
            else:
                record = ImageDB(user=current_user.username, name=image_name, tag=image_tag,
                                 repository=repository, pull_ref=pull_ref, size_bytes=total,
                                 visibility=image_visibility, project=image_project, group=image_group)
                db.add(record)
            db.commit()
            db.refresh(record)
            result = serialize_image_record(record)
        finally:
            db.close()
        return {"message": "Image uploaded", "image": result}
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

@app.get("/images", dependencies=[Depends(check_role(["user", "poweruser", "admin", "readonly"]))])
def list_images(current_user: UserDB = Depends(get_current_user)):
    db = SessionLocal()
    try:
        images = db.query(ImageDB).order_by(ImageDB.created_at.desc()).all()
        result = [serialize_image_record(img) for img in images if can_view_image(img, current_user, db)]
        return {"images": result}
    finally:
        db.close()

@app.delete("/images/{image_id}", dependencies=[Depends(check_role(["user", "poweruser", "admin"]))])
def delete_image(image_id: int, current_user: UserDB = Depends(get_current_user)):
    db = SessionLocal()
    try:
        img = db.query(ImageDB).filter(ImageDB.id == image_id).first()
        if not img:
            raise HTTPException(status_code=404, detail="Image not found")
        if current_user.role != "admin" and img.user != current_user.username:
            raise HTTPException(status_code=403, detail="Not your image")
        db.delete(img)
        db.commit()
        return {"message": "Image record deleted", "id": image_id}
    finally:
        db.close()

@app.put("/images/{image_id}", dependencies=[Depends(check_role(["user", "poweruser", "admin"]))])
def update_image(image_id: int, upd: ImageUpdate, current_user: UserDB = Depends(get_current_user)):
    db = SessionLocal()
    try:
        img = db.query(ImageDB).filter(ImageDB.id == image_id).first()
        if not img:
            raise HTTPException(status_code=404, detail="Image not found")
        if current_user.role != "admin" and img.user != current_user.username:
            raise HTTPException(status_code=403, detail="Only the uploader or admin can edit image visibility")
        visibility, project, group = normalize_image_visibility(upd.visibility or img.visibility or "user",
                                                               upd.project if upd.project is not None else img.project,
                                                               upd.group if upd.group is not None else img.group,
                                                               current_user)
        img.visibility = visibility
        img.project = project
        img.group = group
        db.commit()
        db.refresh(img)
        return {"message": "Image updated", "image": serialize_image_record(img)}
    finally:
        db.close()

# ---------- Pobieranie wyników zadań ----------
def resolve_results_owner(task_name: str, current_user: "UserDB"):
    """Zwraca właściciela (username) wyników danego zadania, egzekwując izolację.

    - Admin: znajduje właściciela po etykiecie poda task-name w dowolnym namespace; gdy brak -> sam admin.
    - Zwykły użytkownik: wynik MUSI należeć do niego. Sprawdzamy, czy pod task-<name> istnieje
      w jego namespace LUB czy w jego katalogu wyników jest podkatalog zadania; w przeciwnym razie 404.
    """
    if LOCAL_DEV_MODE:
        db = SessionLocal()
        try:
            task = db.query(TaskDB).filter(TaskDB.name == task_name).first()
            if not task:
                raise HTTPException(status_code=404, detail="Task not found")
            if current_user.role != "admin" and task.user != current_user.username:
                raise HTTPException(status_code=404, detail="Task not found")
            return task.user or current_user.username
        finally:
            db.close()
    load_kubernetes_config()
    v1 = kubernetes.client.CoreV1Api()
    if current_user.role == "admin":
        try:
            pods = v1.list_pod_for_all_namespaces(label_selector=f"task-name={task_name}")
            if pods.items:
                return (pods.items[0].metadata.labels or {}).get("user", current_user.username)
        except Exception:
            pass
        # Pod już nie istnieje (a wyniki ŻYJĄ dalej na PVC — to cały sens tej funkcji).
        # Pytamy więc o Task CR, który przeżywa poda, żeby ustalić prawdziwego właściciela.
        # Bez tego admin czytający wyniki CUDZEGO zadania po usunięciu poda dostawał
        # własny (pusty) katalog wyników i widział "files": [] zamiast danych użytkownika.
        try:
            objects = kubernetes.client.CustomObjectsApi().list_cluster_custom_object(
                group="devops.local", version="v1", plural="tasks")
            for obj in objects.get("items", []):
                if (obj.get("metadata") or {}).get("name") == task_name:
                    owner = (obj.get("spec") or {}).get("user")
                    if owner:
                        return owner
                    ns_name = (obj.get("metadata") or {}).get("namespace") or ""
                    if ns_name.startswith("user-"):
                        return ns_name[len("user-"):]
        except Exception as exc:
            logger.debug("Could not resolve task owner from Task CRs for %s: %s", task_name, exc)
        # Ani pod, ani Task CR nie istnieje. Zanim uznamy, że chodzi o własne wyniki admina,
        # sprawdzamy, czy taki katalog wyników w ogóle u niego jest. Bez tego GET wyników
        # NIEISTNIEJĄCEGO zadania zwracał adminowi 200 z "files": [] (bo listowaliśmy pusty
        # katalog), zamiast 404 — literówka w nazwie zadania wyglądała jak "brak wyników".
        try:
            nsh, pod = ensure_results_helper(current_user.username)
            out = helper_exec(nsh, pod, ["sh", "-c",
                                         f"test -d {RESULTS_DIR}/{task_name} && echo yes || echo no"])
        except Exception:
            return current_user.username     # nie da się sprawdzić — nie blokujemy admina
        if "yes" in out:
            return current_user.username
        raise HTTPException(status_code=404, detail="Task results not found")
    # Non-admin: weryfikujemy własność.
    ns = f"user-{current_user.username}"
    try:
        v1.read_namespaced_pod(f"task-{task_name}", ns)
        return current_user.username           # pod nadal istnieje w ich namespace
    except kubernetes.client.exceptions.ApiException:
        pass
    # Pod mógł zostać już usunięty — sprawdzamy katalog wyników na ich PVC.
    # Izolacja: przy JAKIMKOLWIEK niepowodzeniu (helper niedostępny, błąd exec) odmawiamy
    # dostępu jako 404, zamiast przeciekać 500/503 przy próbie sięgnięcia po cudze wyniki.
    try:
        nsh, pod = ensure_results_helper(current_user.username)
        out = helper_exec(nsh, pod, ["sh", "-c", f"test -d {RESULTS_DIR}/{task_name} && echo yes || echo no"])
    except Exception:
        raise HTTPException(status_code=404, detail="Task results not found")
    if "yes" in out:
        return current_user.username
    raise HTTPException(status_code=404, detail="Task results not found")

def ensure_results_helper(username: str):
    """Zapewnia długo żyjący pod-pomocnik montujący PVC wyników użytkownika i zwraca jego nazwę.

    Pod (busybox 'sleep') montuje <user>-scratch pod RESULTS_DIR; przez exec listujemy/pakujemy pliki.
    """
    load_kubernetes_config()
    v1 = kubernetes.client.CoreV1Api()
    ns = f"user-{username}"
    pod_name = "results-helper"
    pvc = f"{username}-{RESULTS_PVC_SUFFIX}"
    # Jeden wspólny deadline na CAŁE przygotowanie helpera (delete + create + ready),
    # żeby suma etapów nigdy nie przekroczyła timeoutu klienta.
    deadline = time.monotonic() + RESULTS_HELPER_BUDGET_SECONDS

    def _budget_left() -> float:
        return deadline - time.monotonic()

    def _stamp_last_used():
        # Odświeża znacznik czasu, by reaper nie usunął właśnie używanego poda.
        try:
            v1.patch_namespaced_pod(pod_name, ns, {"metadata": {"annotations": {
                HELPER_LAST_USED_ANNOTATION: datetime.utcnow().isoformat()}}})
        except Exception as exc:
            logger.debug("Could not stamp results-helper last-used in %s: %s", ns, exc)

    def _delete_and_wait_gone():
        try:
            v1.delete_namespaced_pod(pod_name, ns, grace_period_seconds=0)
        except kubernetes.client.exceptions.ApiException as e:
            if e.status != 404:
                raise
        # Reserve at least half of the remaining request budget for starting the replacement Pod.
        delete_deadline = time.monotonic() + max(1.0, min(10.0, _budget_left() / 2))
        while time.monotonic() < delete_deadline:
            try:
                v1.read_namespaced_pod(pod_name, ns)
                time.sleep(1)
            except kubernetes.client.exceptions.ApiException:
                return

    try:
        pod = v1.read_namespaced_pod(pod_name, ns)
        current_image = pod.spec.containers[0].image if (pod.spec and pod.spec.containers) else None
        waiting_reasons = {
            cs.state.waiting.reason for cs in (pod.status.container_statuses or [])
            if cs.state and cs.state.waiting and cs.state.waiting.reason
        }
        stuck = bool(waiting_reasons & {"ImagePullBackOff", "ErrImagePull", "CrashLoopBackOff",
                                        "ErrImageNeverPull", "InvalidImageName"})
        if pod.status.phase == "Running" and current_image == RESULTS_READER_IMAGE and not stuck:
            _stamp_last_used()
            return ns, pod_name
        # Odtwórz poda, gdy: ma nieaktualny obraz (np. po zmianie RESULTS_READER_IMAGE na obraz
        # z rejestru klastra), jest w stanie terminalnym, lub UTKNĄŁ w pobieraniu obrazu
        # (ImagePullBackOff). Bez tego zawieszony helper z poprzedniego wdrożenia trwałby wiecznie.
        if (current_image != RESULTS_READER_IMAGE or stuck
                or pod.status.phase in ("Succeeded", "Failed")):
            _delete_and_wait_gone()
    except kubernetes.client.exceptions.ApiException as e:
        if e.status != 404:
            raise
    body = {
        "metadata": {"name": pod_name, "labels": {"app": "results-helper", "user": username},
                     "annotations": {HELPER_LAST_USED_ANNOTATION: datetime.utcnow().isoformat()}},
        "spec": {
            "restartPolicy": "Never",
            "containers": [{
                "name": "helper",
                "image": RESULTS_READER_IMAGE,
                "command": ["sh", "-c", "sleep 86400"],
                "volumeMounts": [{"name": "results", "mountPath": RESULTS_DIR}],
            }],
            "volumes": [{"name": "results", "persistentVolumeClaim": {"claimName": pvc}}],
        },
    }
    try:
        v1.create_namespaced_pod(namespace=ns, body=body)
    except kubernetes.client.exceptions.ApiException as e:
        if e.status != 409:
            raise
    # Czekamy aż pod wstanie ORAZ kontener będzie ready (Running sam w sobie nie gwarantuje,
    # że exec zadziała). Pierwsze uruchomienie może wymagać pobrania obrazu (busybox), więc
    # dajemy więcej czasu — inaczej pojawiały się wyścigi (500/503) przy odczycie wyników.
    last_reason = ""
    for _ in range(RESULTS_HELPER_READY_TRIES):
        if _budget_left() <= 0:
            break
        try:
            pod = v1.read_namespaced_pod(pod_name, ns)
        except kubernetes.client.exceptions.ApiException:
            time.sleep(2); continue
        phase = pod.status.phase
        statuses = pod.status.container_statuses or []
        ready = bool(statuses) and all(cs.ready for cs in statuses)
        if phase == "Running" and ready:
            _stamp_last_used()
            return ns, pod_name
        if phase in ("Failed", "Succeeded"):
            # Nie da się z tego czytać — skasuj, by kolejna próba odtworzyła poda.
            try:
                v1.delete_namespaced_pod(pod_name, ns)
            except Exception:
                pass
            break
        # Zbierz powód oczekiwania (np. ImagePullBackOff) do komunikatu diagnostycznego.
        for cs in statuses:
            waiting = getattr(cs.state, "waiting", None) if cs.state else None
            if waiting and waiting.reason:
                last_reason = waiting.reason
        # Pod może w ogóle nie mieć containerStatuses — np. gdy jest NIESCHEDULOWALNY, bo
        # PVC <user>-scratch nie jest Bound (zła/nieistniejąca klasa storage). Wtedy powyższa
        # pętla nic nie zbierze i klient dostawał bezużyteczne "not ready" bez przyczyny.
        # Czytamy więc warunek PodScheduled i pokazujemy jego reason/message.
        if not last_reason:
            for cond in (pod.status.conditions or []):
                if cond.type == "PodScheduled" and cond.status != "True":
                    last_reason = ": ".join(x for x in (cond.reason, cond.message) if x)
                    break
        time.sleep(2)
    detail = "Results helper not ready" + (f" ({last_reason})" if last_reason else "")
    raise HTTPException(status_code=503, detail=detail)

class ResultTooLarge(Exception):
    """Wynik przekroczył MAX_RESULT_DOWNLOAD_MB — przerywamy zamiast zjadać RAM API."""


def helper_exec(ns: str, pod_name: str, command: list, binary: bool = False,
                max_bytes: Optional[int] = None):
    """Uruchamia komendę w podzie-pomocniku i zwraca stdout (str lub bytes).

    Exec przez WebSocket bywa przejściowo zawodny (pod dopiero co wstał, chwilowy błąd
    streamu). Ponawiamy kilka razy; przy trwałym niepowodzeniu zwracamy 503 (retryable)
    zamiast 500, dzięki czemu endpointy wyników nie wywalają się na przejściowych błędach.

    `max_bytes` ogranicza rozmiar zebranego stdout. Bez tego `cat`/`tar` dużego pliku
    wczytywał się CAŁY do pamięci procesu API (MAX_RESULT_DOWNLOAD_MB było zadeklarowane
    i udokumentowane, ale nigdzie nie używane) — jeden duży wynik mógł wywrócić API.
    """
    load_kubernetes_config()
    v1 = kubernetes.client.CoreV1Api()
    last_exc = None
    for attempt in range(RESULTS_HELPER_EXEC_RETRIES):
        try:
            resp = k8s_stream(
                v1.connect_get_namespaced_pod_exec, pod_name, ns,
                command=command, container="helper",
                stderr=True, stdin=False, stdout=True, tty=False,
                _preload_content=False,
            )
            out = bytearray()
            try:
                while resp.is_open():
                    resp.update(timeout=RESULTS_HELPER_EXEC_TIMEOUT)
                    if resp.peek_stdout():
                        chunk = resp.read_stdout()
                        out.extend(chunk.encode("utf-8", errors="surrogateescape")
                                   if isinstance(chunk, str) else chunk)
                        if max_bytes is not None and len(out) > max_bytes:
                            raise ResultTooLarge()
                    if resp.peek_stderr():
                        resp.read_stderr()  # pomijamy stderr w treści
            finally:
                resp.close()
            return bytes(out) if binary else bytes(out).decode("utf-8", errors="replace")
        except ResultTooLarge:
            # Twardy limit — ponawianie nic nie da, zwracamy jednoznaczny 413.
            raise HTTPException(
                status_code=413,
                detail=f"Result exceeds MAX_RESULT_DOWNLOAD_MB ({MAX_RESULT_DOWNLOAD_MB} MB)")
        except Exception as exc:
            last_exc = exc
            logger.warning("results-helper exec attempt %d/%d failed in %s: %s",
                           attempt + 1, RESULTS_HELPER_EXEC_RETRIES, ns, exc)
            if attempt + 1 < RESULTS_HELPER_EXEC_RETRIES:
                time.sleep(1)
    raise HTTPException(status_code=503, detail=f"Results helper exec failed: {last_exc}")

@app.get("/tasks/{task_name}/results", dependencies=[Depends(check_role(["user", "poweruser", "admin", "readonly"]))])
def list_results(task_name: str, current_user: UserDB = Depends(get_current_user)):
    """Lista plików wyników danego zadania (z trwałego PVC)."""
    if LOCAL_DEV_MODE:
        resolve_results_owner(task_name, current_user)
        result_data = f"local-dev result for {task_name}\n".encode("utf-8")
        files = [] if task_name in LOCAL_DEV_DELETED_RESULTS else [
            {"size": len(result_data), "size_bytes": len(result_data), "path": "stdout.txt"}
        ]
        return {"task": task_name, "results_dir": f"local-dev/{task_name}", "files": files}
    owner = resolve_results_owner(task_name, current_user)
    ns, pod_name = ensure_results_helper(owner)
    base = f"{RESULTS_DIR}/{task_name}"
    listing = helper_exec(ns, pod_name, ["sh", "-c", f"cd {base} 2>/dev/null && find . -type f -exec ls -la {{}} \\; || true"])
    files = []
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) >= 9:
            # `find .` zwraca ścieżki jak "./out.txt" / "./sub/out.txt". Trzeba obciąć DOKŁADNIE
            # prefiks "./", a nie użyć lstrip("./") — lstrip usuwa ZBIÓR ZNAKÓW, więc plik
            # ".hidden" stawał się "hidden" i potem nie dawał się pobrać (404).
            rel = parts[-1]
            if rel.startswith("./"):
                rel = rel[2:]
            files.append({"size": parts[4], "path": rel})
    return {"task": task_name, "results_dir": base, "files": files}

@app.get("/tasks/{task_name}/results/download", dependencies=[Depends(check_role(["user", "poweruser", "admin", "readonly"]))])
def download_results(task_name: str, path: Optional[str] = None, current_user: UserDB = Depends(get_current_user)):
    """Pobiera wyniki: pojedynczy plik (?path=...) lub cały katalog jako archiwum .tar."""
    # Walidacja ścieżki PRZED jakąkolwiek pracą (owner/helper) — inaczej niegotowy
    # results-helper (503) maskowałby błąd 400 dla prób wyjścia poza katalog wyników.
    if path is not None and (".." in path or path.startswith("/")):
        raise HTTPException(status_code=400, detail="Invalid path")
    if LOCAL_DEV_MODE:
        resolve_results_owner(task_name, current_user)
        if task_name in LOCAL_DEV_DELETED_RESULTS:
            raise HTTPException(status_code=404, detail="Results not found")
        data = f"local-dev result for {task_name}\n".encode("utf-8")
        if path:
            if path != "stdout.txt":
                raise HTTPException(status_code=404, detail="Result file not found")
            return StreamingResponse(
                iter([data]),
                media_type="application/octet-stream",
                headers={"Content-Disposition": "attachment; filename=stdout.txt"},
            )
        archive_buffer = io.BytesIO()
        with tarfile.open(fileobj=archive_buffer, mode="w") as archive:
            info = tarfile.TarInfo(name="stdout.txt")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        return StreamingResponse(
            iter([archive_buffer.getvalue()]),
            media_type="application/x-tar",
            headers={"Content-Disposition": f"attachment; filename={task_name}-results.tar"},
        )
    owner = resolve_results_owner(task_name, current_user)
    ns, pod_name = ensure_results_helper(owner)
    base = f"{RESULTS_DIR}/{task_name}"
    max_bytes = MAX_RESULT_DOWNLOAD_MB * 1024 * 1024
    if path:
        # (walidacja ścieżki wykonana na początku funkcji)
        target = f"{base}/{path}"
        data = helper_exec(ns, pod_name, ["sh", "-c", f"cat {target}"], binary=True,
                           max_bytes=max_bytes)
        fname = os.path.basename(path)
        return StreamingResponse(iter([data]), media_type="application/octet-stream",
                                 headers={"Content-Disposition": f"attachment; filename={fname}"})
    # Cały katalog jako tar.
    data = helper_exec(ns, pod_name, ["sh", "-c", f"cd {base} 2>/dev/null && tar cf - . || true"],
                       binary=True, max_bytes=max_bytes)
    return StreamingResponse(iter([data]), media_type="application/x-tar",
                             headers={"Content-Disposition": f"attachment; filename={task_name}-results.tar"})

@app.delete("/tasks/{task_name}/results", dependencies=[Depends(check_role(["user", "poweruser", "admin"]))])
def delete_results(task_name: str, current_user: UserDB = Depends(get_current_user)):
    """Usuwa katalog wyników danego zadania."""
    if LOCAL_DEV_MODE:
        resolve_results_owner(task_name, current_user)
        LOCAL_DEV_DELETED_RESULTS.add(task_name)
        return {"message": "Results deleted", "task": task_name, "local_dev": True}
    owner = resolve_results_owner(task_name, current_user)
    ns, pod_name = ensure_results_helper(owner)
    base = f"{RESULTS_DIR}/{task_name}"
    helper_exec(ns, pod_name, ["sh", "-c", f"rm -rf {base}"])
    return {"message": "Results deleted", "task": task_name}

def allocate_reservation(db: Session, res: ReservationCreate, current_user: UserDB) -> Reservation:
    """Validate and add a reservation to an existing transaction without committing it."""
    ensure_schedulable_gpu_uuid(res.gpu_uuid)
    start_time = naive_utc(res.start_time)
    end_time = naive_utc(res.end_time)
    if end_time <= start_time:
        raise HTTPException(status_code=400, detail="End time must be after start time")

    overlapping = db.query(Reservation).filter(
        Reservation.gpu_uuid == res.gpu_uuid,
        Reservation.status == "active",
        Reservation.start_time < end_time,
        Reservation.end_time > start_time,
    ).all()

    slice_index = None
    if res.gpu_partition:
        part = db.query(GPUPartitionDB).filter(GPUPartitionDB.gpu_uuid == res.gpu_uuid).first()
        profiles = json.loads(part.mig_profiles) if (part and part.mig_profiles) else {}
        capacity = int(profiles.get(res.gpu_partition, 0))
        if capacity < 1:
            raise HTTPException(
                status_code=400,
                detail=f"GPU {res.gpu_uuid} is not partitioned into '{res.gpu_partition}' slices",
            )
        if any(not current.gpu_partition for current in overlapping):
            raise HTTPException(status_code=409, detail="GPU reserved as a whole in this slot")
        used_indexes = {
            current.slice_index for current in overlapping
            if current.gpu_partition == res.gpu_partition
        }
        free_indexes = [index for index in range(capacity) if index not in used_indexes]
        if not free_indexes:
            raise HTTPException(
                status_code=409,
                detail=f"All {capacity} '{res.gpu_partition}' slices are reserved",
            )
        slice_index = free_indexes[0]
    elif overlapping:
        blocking = [current for current in overlapping if (current.priority or 0) >= res.priority]
        if blocking:
            raise HTTPException(status_code=409, detail="Time slot conflict")
        for current in overlapping:
            current.status = "preempted"
            owner = db.query(UserDB).filter(UserDB.id == current.user_id).first()
            notify_user(
                owner,
                "Reservation preempted",
                f"Your reservation {current.id} for GPU {current.gpu_uuid} was preempted by a higher-priority reservation.",
            )

    reservation = Reservation(
        user_id=current_user.id,
        gpu_uuid=res.gpu_uuid,
        start_time=start_time,
        end_time=end_time,
        priority=res.priority,
        gpu_partition=res.gpu_partition,
        slice_index=slice_index,
    )
    db.add(reservation)
    return reservation


@app.post("/reservations", dependencies=[Depends(check_role(["user", "poweruser", "admin"]))])
def create_reservation(res: ReservationCreate, current_user: UserDB = Depends(get_current_user)):
    db = SessionLocal()
    try:
        reservation = allocate_reservation(db, res, current_user)
        db.commit()
        db.refresh(reservation)
        return {
            "message": "Reservation created",
            "id": reservation.id,
            "gpu_partition": reservation.gpu_partition,
            "slice_index": reservation.slice_index,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

@app.put("/reservations/{reservation_id}/renew", dependencies=[Depends(check_role(["user", "poweruser", "admin"]))])
def renew_reservation(reservation_id: int, body: ReservationRenew, current_user: UserDB = Depends(get_current_user)):
    """Extend an active reservation to a new end time."""
    db = SessionLocal()
    try:
        reservation = db.query(Reservation).filter(Reservation.id == reservation_id).first()
        if not reservation:
            raise HTTPException(status_code=404, detail="Reservation not found")
        # Tylko właściciel lub admin może odnowić rezerwację.
        if current_user.role != "admin" and reservation.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="Not your reservation")
        if reservation.status != "active":
            raise HTTPException(status_code=400, detail="Only active reservations can be renewed")
        new_end = naive_utc(body.end_time)
        if new_end <= reservation.end_time:
            raise HTTPException(status_code=400, detail="New end time must be after current end time")
        # Sprawdzamy konflikt z innymi rezerwacjami tej samej karty w przedłużanym oknie.
        conflict = db.query(Reservation).filter(
            Reservation.id != reservation.id,
            Reservation.gpu_uuid == reservation.gpu_uuid,
            Reservation.status == "active",
            Reservation.start_time < new_end,
            Reservation.end_time > reservation.end_time,
        ).first()
        if conflict:
            raise HTTPException(status_code=409, detail="Time slot conflict")
        reservation.end_time = new_end
        db.commit()
        db.refresh(reservation)
        result = {"message": "Reservation renewed", "id": reservation.id, "end_time": reservation.end_time.isoformat()}
        return result
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.get("/reservations")
def list_reservations(current_user: UserDB = Depends(get_current_user)):
    """Lista rezerwacji bieżącego użytkownika (lub wszystkich dla admina) – dla frontendu."""
    db = SessionLocal()
    query = db.query(Reservation)
    if current_user.role != "admin":
        query = query.filter(Reservation.user_id == current_user.id)
    reservations = query.order_by(Reservation.start_time.desc()).all()
    user_ids = {r.user_id for r in reservations}
    users = {u.id: u.username for u in db.query(UserDB).filter(UserDB.id.in_(user_ids)).all()} if user_ids else {}
    result = [
        {
            "id": r.id,
            "user": users.get(r.user_id, r.user_id),
            "gpu_uuid": r.gpu_uuid,
            "start_time": r.start_time.isoformat() if r.start_time else None,
            "end_time": r.end_time.isoformat() if r.end_time else None,
            "status": r.status,
            "priority": r.priority or 0,
            "gpu_partition": r.gpu_partition,
            "slice_index": r.slice_index,
            "notified_start": r.notified_start or False,
            "notified_end": r.notified_end or False,
        }
        for r in reservations
    ]
    db.close()
    return {"reservations": result}

@app.delete("/reservations/{reservation_id}", dependencies=[Depends(check_role(["user", "poweruser", "admin"]))])
def cancel_reservation(reservation_id: int, current_user: UserDB = Depends(get_current_user)):
    """Anuluje rezerwację (właściciel lub admin) – dla frontendu."""
    db = SessionLocal()
    try:
        reservation = db.query(Reservation).filter(Reservation.id == reservation_id).first()
        if not reservation:
            raise HTTPException(status_code=404, detail="Reservation not found")
        if current_user.role != "admin" and reservation.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="Not your reservation")
        reservation.status = "cancelled"
        # Symetria: anuluj powiązany job i usuń jego działające zadanie (pod).
        stopped_task = None
        job = db.query(JobDB).filter(JobDB.reservation_id == reservation.id).first()
        if job:
            if job.status in ("scheduled", "submitted"):
                job.status = "cancelled"
            if job.task_name:
                stopped_task = job.task_name
                delete_task_cr(job.username, job.task_name)
        db.commit()
        return {"message": "Reservation cancelled", "id": reservation_id, "stopped_task": stopped_task}
    except HTTPException:
        db.rollback()
        raise
    finally:
        db.close()

@app.post("/reservations/{reservation_id}/send-reminder", dependencies=[Depends(check_role(["user", "poweruser", "admin"]))])
def send_reminder(reservation_id: int, type: str = "start", current_user: UserDB = Depends(get_current_user)):
    """Ręcznie wysyła powiadomienie o rezerwacji (wiadomość email/Slack).

    Parametry:
    - type: "start" lub "end" – wskazuje jaki typ przypomnienia wysłać
    """
    if type not in ("start", "end"):
        raise HTTPException(status_code=400, detail="Type must be 'start' or 'end'")

    db = SessionLocal()
    try:
        reservation = db.query(Reservation).filter(Reservation.id == reservation_id).first()
        if not reservation:
            raise HTTPException(status_code=404, detail="Reservation not found")
        if current_user.role != "admin" and reservation.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="Not your reservation")

        user = db.query(UserDB).filter(UserDB.id == reservation.user_id).first()
        if not user:
            return {"message": "User not found"}

        # Import send_notification – sprawdzi dostępność email/Slack i wyśle wiadomość
        from send_notification import send_notification

        if type == "start":
            notification_text = f"GPU {reservation.gpu_uuid} reservation starts at {reservation.start_time}"
            send_notification(user, notification_text)
            reservation.notified_start = True
        else:
            notification_text = f"GPU {reservation.gpu_uuid} reservation ends at {reservation.end_time}"
            send_notification(user, notification_text)
            reservation.notified_end = True

        db.commit()
        return {"message": "Reminder sent", "type": type, "reservation_id": reservation_id}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error("Error sending reminder: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to send reminder: {str(e)}")
    finally:
        db.close()

@app.get("/user-preferences", dependencies=[Depends(check_role(["user", "poweruser", "admin"]))])
def get_user_preferences(current_user: UserDB = Depends(get_current_user)):
    """Pobiera preferencje użytkownika dot. powiadomień (np. czas przed przypomnieniem)."""
    db = SessionLocal()
    try:
        prefs = db.query(UserPreferences).filter(UserPreferences.user_id == current_user.id).first()
        if not prefs:
            # Utwórz domyślne preferencje
            prefs = UserPreferences(user_id=current_user.id, reminder_lead_time_minutes=5)
            db.add(prefs)
            db.commit()
        return {
            "reminder_lead_time_minutes": prefs.reminder_lead_time_minutes,
        }
    finally:
        db.close()

@app.post("/user-preferences", dependencies=[Depends(check_role(["user", "poweruser", "admin"]))])
def update_user_preferences(
    reminder_lead_time_minutes: int,
    current_user: UserDB = Depends(get_current_user)
):
    """Aktualizuje preferencje użytkownika dot. powiadomień."""
    if not (1 <= reminder_lead_time_minutes <= 60):
        raise HTTPException(status_code=400, detail="Lead time must be between 1 and 60 minutes")

    db = SessionLocal()
    try:
        prefs = db.query(UserPreferences).filter(UserPreferences.user_id == current_user.id).first()
        if not prefs:
            prefs = UserPreferences(user_id=current_user.id)
            db.add(prefs)
        prefs.reminder_lead_time_minutes = reminder_lead_time_minutes
        db.commit()
        return {
            "message": "Preferences updated",
            "reminder_lead_time_minutes": prefs.reminder_lead_time_minutes,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error("Error updating user preferences: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to update preferences: {str(e)}")
    finally:
        db.close()

@app.get("/availability", dependencies=[Depends(check_role(["user", "poweruser", "admin", "readonly"]))])
def availability(start_time: datetime, end_time: datetime):
    """Zwraca dostępność każdej karty GPU w zadanym oknie czasu (do planowania rezerwacji).

    Karta jest 'available', jeśli w oknie nie ma aktywnej rezerwacji CAŁEJ karty
    (rezerwacje na slice MIG nie blokują całej karty). `busy_windows` to zajęte przedziały.
    """
    start = naive_utc(start_time)
    end = naive_utc(end_time)
    if end <= start:
        raise HTTPException(status_code=400, detail="End time must be after start time")
    gpus = list_gpus().get("gpus", [])
    db = SessionLocal()
    try:
        result = []
        for g in gpus:
            uuid = g.get("uuid")
            overlapping = db.query(Reservation).filter(
                Reservation.gpu_uuid == uuid,
                Reservation.status == "active",
                Reservation.start_time < end,
                Reservation.end_time > start,
            ).all()
            whole_card_taken = any(not r.gpu_partition for r in overlapping)
            result.append({
                "gpu_uuid": uuid,
                "product": g.get("product"),
                "node": g.get("node"),
                "synthetic_e2e": bool(g.get("simulated")),
                "available": not whole_card_taken,
                "busy_windows": [
                    {"start": r.start_time.isoformat(), "end": r.end_time.isoformat()}
                    for r in overlapping
                ],
            })
        # Echo the queried window back: klient (UI/testy) dostaje potwierdzenie, dla jakiego
        # przedziału policzono dostępność, bez zgadywania po własnych parametrach.
        return {"start_time": start.isoformat() + "Z", "end_time": end.isoformat() + "Z",
                "gpus": result}
    finally:
        db.close()

@app.get("/reservations/calendar")
def calendar(gpu_uuid: Optional[str] = None):
    db = SessionLocal()
    query = db.query(Reservation).filter(Reservation.status == "active")
    if gpu_uuid:
        query = query.filter(Reservation.gpu_uuid == gpu_uuid)
    reservations = query.all()
    user_ids = {r.user_id for r in reservations}
    users = {u.id: u.username for u in db.query(UserDB).filter(UserDB.id.in_(user_ids)).all()} if user_ids else {}
    result = [
        {
            "id": r.id,
            "title": f"User: {users.get(r.user_id, r.user_id)}",
            "user": users.get(r.user_id, r.user_id),
            "start": r.start_time.isoformat(),
            "end": r.end_time.isoformat(),
            "resourceId": r.gpu_uuid,
            "status": r.status,
        }
        for r in reservations
    ]
    db.close()
    return result

def _task_gpu_count(task: TaskCreate):
    """Ile GPU żąda zadanie (z resources.limits['nvidia.com/gpu'], domyślnie 0/1 wg partycji)."""
    try:
        req = int((task.resources or {}).get("limits", {}).get("nvidia.com/gpu", 0) or 0)
    except (ValueError, TypeError):
        req = 0
    if task.gpu_partition:           # slice MIG liczymy jako 1 jednostkę GPU
        req = max(req, 1)
    return req

def _pod_gpu_units(pod):
    """Liczba jednostek GPU używanych przez poda (całe karty + slice MIG)."""
    n = 0
    for c in pod.spec.containers:
        lim = (c.resources.limits or {}) if c.resources else {}
        n += int(lim.get("nvidia.com/gpu", 0) or 0)
        n += sum(int(v or 0) for k, v in lim.items() if k.startswith("nvidia.com/mig-"))
    return n

def normalize_gpu_type(product: str):
    """Normalizuje nazwę modelu karty do klucza limitu (spacje -> '-')."""
    return (product or "unknown").strip().replace(" ", "-")

def resolve_task_gpu_type(task: TaskCreate):
    """Ustala typ (model) GPU dla zadania: jawny gpu_type, albo wykryty z gpu_uuid (capabilities/partycja)."""
    if task.gpu_type:
        return normalize_gpu_type(task.gpu_type)
    if task.gpu_uuid:
        try:
            for c in detect_gpu_capabilities():
                if c["gpu_uuid"] == task.gpu_uuid:
                    return normalize_gpu_type(c.get("product"))
        except Exception:
            pass
    return "default"

def gpus_in_use_by_type(usernames=None, project=None):
    """Zwraca {typ_gpu: liczba_jednostek} aktualnie używanych. Typ z etykiety poda gpu-type."""
    by_type = {}
    try:
        load_kubernetes_config()
        v1 = kubernetes.client.CoreV1Api()
        pods = []
        if usernames is not None:
            for u in usernames:
                pods += v1.list_namespaced_pod(f"user-{u}", label_selector="app=task").items
        elif project is not None:
            pods += v1.list_pod_for_all_namespaces(label_selector=f"app=task,project={project}").items
        for pod in pods:
            if pod.status.phase not in ("Pending", "Running"):
                continue
            t = (pod.metadata.labels or {}).get("gpu-type", "default")
            by_type[t] = by_type.get(t, 0) + _pod_gpu_units(pod)
    except Exception as exc:
        logger.debug("gpus_in_use_by_type probe failed: %s", exc)
    return by_type

def _quota_for_type(by_type_json, scalar_default, gpu_type):
    """Zwraca limit dla danego typu: mapa[typ] -> mapa['default'] -> skalarny fallback. None = brak limitu."""
    if by_type_json:
        try:
            m = json.loads(by_type_json)
            if gpu_type in m:
                return int(m[gpu_type])
            if "default" in m:
                return int(m["default"])
        except (ValueError, TypeError):
            pass
    return scalar_default

def enforce_quota(current_user: UserDB, task: TaskCreate):
    """Egzekwuje limity PER TYP GPU: VRAM per zadanie (user), oraz liczbę GPU per user/grupa/projekt. Admin zwolniony."""
    if current_user.role == "admin":
        return
    gpu_type = resolve_task_gpu_type(task)

    # VRAM per zadanie — limit zależny od typu karty (4GB na 1050Ti != 4GB na H100).
    if task.vram_limit_gb is not None:
        vram_cap = _quota_for_type(current_user.vram_quota_by_type, current_user.quota_vram_gb, gpu_type)
        if vram_cap is not None and task.vram_limit_gb > vram_cap:
            raise HTTPException(status_code=403,
                detail=f"VRAM request {task.vram_limit_gb}GB exceeds your quota {vram_cap}GB for GPU type '{gpu_type}'")

    want = _task_gpu_count(task)
    if want <= 0:
        return

    # 1) Limit GPU per użytkownik — per typ.
    user_cap = _quota_for_type(current_user.gpu_quota_by_type, current_user.quota_gpu, gpu_type)
    if user_cap is not None:
        used = gpus_in_use_by_type(usernames=[current_user.username]).get(gpu_type, 0)
        if used + want > user_cap:
            raise HTTPException(status_code=403,
                detail=f"User GPU quota for type '{gpu_type}' exceeded: using {used}, requested {want}, quota {user_cap}")

    db = SessionLocal()
    try:
        # 2) Limit per grupa (zespół) — per typ, sumowane po członkach.
        if current_user.team:
            grp = db.query(GroupDB).filter(GroupDB.name == current_user.team).first()
            if grp:
                cap = _quota_for_type(grp.gpu_quota_by_type, grp.total_gpus, gpu_type)
                if cap is not None:
                    members = [u.username for u in db.query(UserDB).filter(UserDB.team == current_user.team).all()]
                    used = gpus_in_use_by_type(usernames=members).get(gpu_type, 0)
                    if used + want > cap:
                        raise HTTPException(status_code=403,
                            detail=f"Group '{current_user.team}' GPU quota for type '{gpu_type}' exceeded: using {used}, requested {want}, quota {cap}")
        # 3) Limit per projekt — per typ.
        if task.project:
            proj = db.query(ProjectDB).filter(ProjectDB.name == task.project).first()
            if proj:
                cap = _quota_for_type(proj.gpu_quota_by_type, proj.total_gpus, gpu_type)
                if cap is not None:
                    used = gpus_in_use_by_type(project=task.project).get(gpu_type, 0)
                    if used + want > cap:
                        raise HTTPException(status_code=403,
                            detail=f"Project '{task.project}' GPU quota for type '{gpu_type}' exceeded: using {used}, requested {want}, quota {cap}")
    finally:
        db.close()

def enforce_quota_for_request(current_user: UserDB, *, image, resources, gpus=0, replicas=1,
                              gpu_uuid=None, vram_limit_gb=None, project=None,
                              gpu_partition=None):
    """Apply task quotas consistently to queued and scheduled workloads.

    GPU count is checked for the entire request (`gpus × replicas`) because all replicas may run concurrently.
    """
    limits = dict(((resources or {}).get("limits") or {}))
    requested = (gpus or 0) * max(1, replicas or 1)
    if requested:
        limits["nvidia.com/gpu"] = requested
    enforce_quota(current_user, TaskCreate(
        image=image,
        resources={"limits": limits},
        gpu_uuid=gpu_uuid,
        vram_limit_gb=vram_limit_gb,
        project=project,
        gpu_partition=gpu_partition,
    ))

def dispatch_task_for_user(username: str, task: TaskCreate):
    task_name = f"task-{uuid.uuid4().hex[:8]}"
    if LOCAL_DEV_MODE:
        db = SessionLocal()
        record = TaskDB(
            name=task_name,
            user=username,
            image=task.image,
            command=json.dumps(task.command),
            resources=json.dumps(task.resources or {}),
            gpu_uuid=task.gpu_uuid,
            time_limit_seconds=task.time_limit_seconds,
            vram_limit_gb=task.vram_limit_gb,
            project=task.project,
            gpu_partition=task.gpu_partition,
            status="submitted",
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        result = {"task": task_name, "record": serialize_task_record(record)}
        db.close()
        return result

    load_kubernetes_config()
    custom_api = kubernetes.client.CustomObjectsApi()
    # Use the task's VRAM limit when provided, otherwise apply the configured default.
    vram_limit_gb = task.vram_limit_gb if task.vram_limit_gb is not None else DEFAULT_VRAM_LIMIT_GB
    # Jeśli zadanie celuje w slice MIG, rzeczywisty VRAM slice'a jest nadrzędny — sprzętowo
    # wymuszony. Ograniczamy (clamp) advisory vramLimitGB do faktycznej wielkości slice'a,
    # aby zmienna VRAM_LIMIT_MB nie "obiecywała" więcej pamięci niż ma slice.
    if task.gpu_partition:
        slice_vram = MIG_PROFILE_VRAM_GB.get(task.gpu_partition)
        if slice_vram:
            if task.vram_limit_gb is None or task.vram_limit_gb > slice_vram:
                vram_limit_gb = slice_vram
    spec = {
        "user": username,
        "image": task.image,
        "command": task.command,
        "resources": task.resources or {},
        "timeLimitSeconds": task.time_limit_seconds,
        "gpuUUID": task.gpu_uuid,
        "vramLimitGB": vram_limit_gb,
    }
    if task.project:
        spec["project"] = task.project
    # Slice binding: gdy zadanie celuje w konkretny profil MIG, kontroler zażąda
    # zasobu nvidia.com/mig-<profil> zamiast nvidia.com/gpu.
    if task.gpu_partition:
        spec["migProfile"] = task.gpu_partition
    # Typ GPU (model) — do liczenia limitów per-typ; pod dostanie etykietę gpu-type.
    spec["gpuType"] = resolve_task_gpu_type(task)
    task_manifest = {
        "apiVersion": "devops.local/v1",
        "kind": "Task",
        "metadata": {"name": task_name},
        "spec": spec,
    }
    custom_api.create_namespaced_custom_object(
        group="devops.local", version="v1", namespace=f"user-{username}",
        plural="tasks", body=task_manifest
    )
    return {"task": task_name}

def dispatch_parallel_job(q: "QueuedJobDB"):
    """Create `replicas` tasks with `gpus` GPUs assigned to each task.

    MPI and gang workloads receive rank and world-size environment variables for coordination.
    Returns the names of the created tasks.
    """
    load_kubernetes_config()
    custom_api = kubernetes.client.CustomObjectsApi()
    command = json.loads(q.command or "[]")
    base_resources = json.loads(q.resources or "{}") or {}
    vram = q.vram_limit_gb if q.vram_limit_gb is not None else DEFAULT_VRAM_LIMIT_GB
    world_size = max(1, int(q.replicas or 1))
    gpus_per = max(0, int(q.gpus or 0))
    group_id = f"q{q.id}-{uuid.uuid4().hex[:6]}"
    created = []
    for rank in range(world_size):
        # Zasoby: nakładamy żądanie GPU per pod (multi-GPU).
        resources = json.loads(json.dumps(base_resources))  # deep copy
        if gpus_per > 0:
            resources.setdefault("limits", {})["nvidia.com/gpu"] = gpus_per
        task_name = f"task-{group_id}-r{rank}"
        spec = {
            "user": q.user,
            "image": q.image,
            "command": command,
            "resources": resources,
            "timeLimitSeconds": q.time_limit_seconds,
            "gpuUUID": q.gpu_uuid,
            "vramLimitGB": vram,
            "jobGroup": group_id,
            # Koordynacja multi-node / MPI:
            "env": [
                {"name": "JOB_GROUP", "value": group_id},
                {"name": "JOB_RANK", "value": str(rank)},
                {"name": "JOB_WORLD_SIZE", "value": str(world_size)},
                {"name": "JOB_MPI", "value": "1" if q.mpi else "0"},
            ],
        }
        if q.project:
            spec["project"] = q.project
        manifest = {
            "apiVersion": "devops.local/v1",
            "kind": "Task",
            "metadata": {"name": task_name, "labels": {"job-group": group_id, "queued-job": str(q.id)}},
            "spec": spec,
        }
        custom_api.create_namespaced_custom_object(
            group="devops.local", version="v1", namespace=f"user-{q.user}",
            plural="tasks", body=manifest)
        created.append(task_name)
    return created

@app.post("/tasks", dependencies=[Depends(check_role(["user", "poweruser", "admin"]))])
def create_task(task: TaskCreate, current_user: UserDB = Depends(get_current_user)):
    audit_note(target=task.image, image=task.image, gpu_uuid=task.gpu_uuid, vram_limit_gb=task.vram_limit_gb,
               project=task.project, gpu_partition=task.gpu_partition, time_limit_seconds=task.time_limit_seconds)
    ensure_schedulable_gpu_uuid(task.gpu_uuid)
    ensure_project_access(task.project, current_user)  # członkostwo w projekcie
    enforce_quota(current_user, task)   # limity GPU/VRAM użytkownika
    try:
        result = dispatch_task_for_user(current_user.username, task)
        return {"message": "Task created", **result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/tasks", dependencies=[Depends(check_role(["user", "poweruser", "admin", "readonly"]))])
def list_tasks(current_user: UserDB = Depends(get_current_user)):
    try:
        if LOCAL_DEV_MODE:
            db = SessionLocal()
            query = db.query(TaskDB)
            if current_user.role != "admin":
                query = query.filter(TaskDB.user == current_user.username)
            tasks = query.order_by(TaskDB.created_at.desc()).all()
            result = [serialize_task_record(task) for task in tasks]
            db.close()
            return {"tasks": result}
        load_kubernetes_config()
        custom_api = kubernetes.client.CustomObjectsApi()
        if current_user.role == "admin":
            objects = custom_api.list_cluster_custom_object(group="devops.local", version="v1", plural="tasks")
        else:
            objects = custom_api.list_namespaced_custom_object(group="devops.local", version="v1", namespace=f"user-{current_user.username}", plural="tasks")
        items = objects.get("items", [])
        outcomes = load_task_outcomes(
            [it.get("metadata", {}).get("name") for it in items]
        )
        tasks = []
        for item in items:
            spec = item.get("spec", {})
            status_obj = item.get("status", {})
            name = item.get("metadata", {}).get("name")
            tasks.append({
                "name": name,
                "user": spec.get("user"),
                "image": spec.get("image"),
                "command": spec.get("command", []),
                "resources": spec.get("resources", {}),
                "gpu_uuid": spec.get("gpuUUID"),
                "time_limit_seconds": spec.get("timeLimitSeconds"),
                "vram_limit_gb": spec.get("vramLimitGB"),
                "project": spec.get("project"),
                "gpu_partition": spec.get("migProfile"),
                "status": status_obj.get("phase", "created"),
                "failure_reason": task_failure_reason(name, outcomes),
                "created_at": item.get("metadata", {}).get("creationTimestamp"),
            })
        return {"tasks": tasks}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/tasks/{task_name}", dependencies=[Depends(check_role(["user", "poweruser", "admin"]))])
def delete_task(task_name: str, current_user: UserDB = Depends(get_current_user)):
    try:
        if LOCAL_DEV_MODE:
            db = SessionLocal()
            query = db.query(TaskDB).filter(TaskDB.name == task_name)
            if current_user.role != "admin":
                query = query.filter(TaskDB.user == current_user.username)
            task = query.first()
            if not task:
                db.close()
                raise HTTPException(status_code=404, detail="Task not found")
            task.status = "cancelled"
            db.commit()
            db.close()
            return {"message": "Task cancelled", "task": task_name}
        load_kubernetes_config()
        custom_api = kubernetes.client.CustomObjectsApi()
        namespace = f"user-{current_user.username}"
        if current_user.role == "admin":
            objects = custom_api.list_cluster_custom_object(group="devops.local", version="v1", plural="tasks")
            match = next((item for item in objects.get("items", []) if item.get("metadata", {}).get("name") == task_name), None)
            if not match:
                raise HTTPException(status_code=404, detail="Task not found")
            namespace = match.get("metadata", {}).get("namespace", namespace)
        custom_api.delete_namespaced_custom_object(group="devops.local", version="v1", namespace=namespace, plural="tasks", name=task_name)
        return {"message": "Task deleted", "task": task_name}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ---------- Łączenie z podem zadania (logi + interaktywny exec) ----------
TASK_OUTCOME_LOG_LINES = 400

def _pod_failure_details(pod):
    """Wyciąga (reason, message, exit_code) z poda: kontener `main`, potem poziom poda.

    Kontener, który nie zdążył wystartować, ma tylko state.terminated/waiting z reason
    typu StartError/CreateContainerError — i zero logów. Pod niezaplanowany ma powód
    dopiero w warunkach (FailedScheduling).
    """
    for status in (pod.status.container_statuses or []):
        if status.name != "main" or not status.state:
            continue
        if status.state.terminated:
            terminated = status.state.terminated
            return terminated.reason, terminated.message, terminated.exit_code
        if status.state.waiting:
            return status.state.waiting.reason, status.state.waiting.message, None
    if pod.status.reason or pod.status.message:
        return pod.status.reason, pod.status.message, None
    for condition in (pod.status.conditions or []):
        if condition.status == "False" and condition.reason:
            return condition.reason, condition.message, None
    return None, None, None

def record_task_outcome(v1, pod, capture_logs=True):
    """Zapisuje przyczynę zakończenia poda zadania i jego ostatnie logi (idempotentnie).

    Wywoływane przez reaper (przed usunięciem poda) i przez pętlę kolejki, żeby awaria
    dała się wyjaśnić także wtedy, gdy pod już nie istnieje.
    """
    task_name = (pod.metadata.labels or {}).get("task-name")
    if not task_name:
        return None
    namespace = pod.metadata.namespace or ""
    user = namespace[len("user-"):] if namespace.startswith("user-") else ""
    reason, message, exit_code = _pod_failure_details(pod)
    logs = None
    if capture_logs:
        try:
            resp = v1.read_namespaced_pod_log(name=pod.metadata.name, namespace=namespace,
                                              tail_lines=TASK_OUTCOME_LOG_LINES,
                                              _preload_content=False)
            raw = resp.data
            logs = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        except Exception:
            # Kontener, który nigdy nie wystartował, nie ma strumienia logów — to nie błąd.
            logs = None
    db = SessionLocal()
    try:
        row = db.query(TaskOutcome).filter(TaskOutcome.task_name == task_name).first()
        if not row:
            row = TaskOutcome(task_name=task_name)
            db.add(row)
        row.user = user or row.user
        row.phase = pod.status.phase or row.phase
        row.reason = reason or row.reason
        row.message = message or row.message
        row.exit_code = exit_code if exit_code is not None else row.exit_code
        if logs:
            row.logs = logs
        row.recorded_at = datetime.utcnow()
        db.commit()
        return row.phase
    except Exception as exc:
        db.rollback()
        logger.warning("Could not record outcome for task %s: %s", task_name, exc)
        return None
    finally:
        db.close()

def _serialize_outcome(row: "TaskOutcome"):
    return {
        "task": row.task_name,
        "user": row.user,
        "phase": row.phase,
        "reason": row.reason,
        "message": row.message,
        "exit_code": row.exit_code,
        "recorded_at": row.recorded_at.isoformat() if row.recorded_at else None,
        "logs": row.logs,
    }

def load_task_outcomes(task_names):
    """Wyniki dla wielu zadań jednym zapytaniem — `GET /queue` odpytuje UI co 3 s."""
    names = [n for n in set(task_names or []) if n]
    if not names:
        return {}
    db = SessionLocal()
    try:
        rows = db.query(TaskOutcome).filter(TaskOutcome.task_name.in_(names)).all()
        return {row.task_name: _serialize_outcome(row) for row in rows}
    finally:
        db.close()

def get_task_outcome(task_name: str):
    return load_task_outcomes([task_name]).get(task_name)

def describe_task_outcome(outcome: dict):
    """Tekst pokazywany w miejscu logów, gdy poda już nie ma."""
    parts = [f"Task {outcome['task']} is no longer running and its pod has been removed."]
    if outcome.get("phase"):
        parts.append(f"Final pod phase: {outcome['phase']}.")
    if outcome.get("reason"):
        parts.append(f"Reason: {outcome['reason']}.")
    if outcome.get("exit_code") is not None:
        parts.append(f"Exit code: {outcome['exit_code']}.")
    if outcome.get("message"):
        parts.append(f"Message: {outcome['message']}")
    if not outcome.get("logs"):
        parts.append(
            "No container output was produced. A container that fails before it starts "
            "(for example when the command is not an executable) never writes any logs."
        )
    if outcome.get("recorded_at"):
        parts.append(f"Recorded at {outcome['recorded_at']}Z.")
    return "\n".join(parts)

def resolve_task_pod(task_name: str, current_user: UserDB):
    """Zwraca (namespace, pod_name) dla zadania, sprawdzając właściciela.

    Pod tworzony przez kontroler nazywa się `task-<task_name>` w namespace `user-<username>`.
    Admin może sięgnąć do dowolnego namespace user-*.
    """
    load_kubernetes_config()
    v1 = kubernetes.client.CoreV1Api()
    namespace = f"user-{current_user.username}"
    if current_user.role == "admin":
        # Znajdź pod po etykiecie task-name w dowolnym namespace użytkownika.
        pods = v1.list_pod_for_all_namespaces(label_selector=f"task-name={task_name}")
        if pods.items:
            p = pods.items[0]
            return p.metadata.namespace, p.metadata.name
    pod_name = f"task-{task_name}"
    try:
        v1.read_namespaced_pod(name=pod_name, namespace=namespace)
    except kubernetes.client.exceptions.ApiException:
        raise HTTPException(status_code=404, detail="Task pod not found")
    return namespace, pod_name

@app.get("/tasks/{task_name}/logs", dependencies=[Depends(check_role(["user", "poweruser", "admin", "readonly"]))])
def task_logs(task_name: str, follow: bool = False, tail_lines: int = 200,
              current_user: UserDB = Depends(get_current_user)):
    """Zwraca (lub streamuje) logi poda zadania."""
    if LOCAL_DEV_MODE:
        resolve_results_owner(task_name, current_user)
        text = f"local-dev logs for {task_name}\nfollow={follow} tail_lines={tail_lines}\n"
        if follow:
            return StreamingResponse(iter([text]), media_type="text/plain")
        return {"task": task_name, "pod": f"local-dev-{task_name}", "logs": text}
    try:
        namespace, pod_name = resolve_task_pod(task_name, current_user)
    except HTTPException as exc:
        # Pod już nie istnieje. Jeżeli zapisaliśmy, jak się skończył, to jest właśnie ta
        # informacja, po którą sięga użytkownik — zwracamy ją zamiast samego 404.
        if exc.status_code != 404:
            raise
        outcome = get_task_outcome(task_name)
        if not outcome:
            raise
        # Izolacja: właściciel zapisany razem z wynikiem (z namespace poda). Nie pytamy
        # o katalog wyników, bo zadanie, które nie wystartowało, żadnego nie ma.
        if current_user.role != "admin" and outcome.get("user") != current_user.username:
            raise HTTPException(status_code=404, detail="Task pod not found")
        text = outcome.get("logs") or ""
        body = f"{text}\n{describe_task_outcome(outcome)}" if text else describe_task_outcome(outcome)
        if follow:
            return StreamingResponse(iter([body]), media_type="text/plain")
        return {"task": task_name, "pod": None, "logs": body, "outcome": outcome}
    v1 = kubernetes.client.CoreV1Api()
    if not follow:
        try:
            resp = v1.read_namespaced_pod_log(name=pod_name, namespace=namespace, tail_lines=tail_lines,
                                              _preload_content=False)
            raw = resp.data
            if isinstance(raw, bytes):
                logs = raw.decode("utf-8", errors="replace")
            else:
                logs = str(raw)
            return {"task": task_name, "pod": pod_name, "logs": logs}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    def log_stream():
        resp = v1.read_namespaced_pod_log(name=pod_name, namespace=namespace, follow=True,
                                          tail_lines=tail_lines, _preload_content=False)
        try:
            for line in resp.stream():
                yield line
        finally:
            resp.release_conn()
    return StreamingResponse(log_stream(), media_type="text/plain")

def ws_user_from_token(token: str):
    """Weryfikuje token (z query param) dla połączeń WebSocket i zwraca użytkownika."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if not username:
            return None
        db = SessionLocal()
        user = db.query(UserDB).filter(UserDB.username == username).first()
        db.close()
        return user
    except jwt.PyJWTError:
        return None

@app.websocket("/tasks/{task_name}/exec")
async def task_exec(websocket: WebSocket, task_name: str, token: str = Query(...),
                    command: str = Query("/bin/sh")):
    """Interaktywna powłoka w podzie zadania przez WebSocket.

    Klient łączy się z `?token=<JWT>&command=/bin/sh`, wysyła stdin jako wiadomości tekstowe,
    odbiera stdout/stderr jako wiadomości tekstowe.
    """
    await websocket.accept()
    user = ws_user_from_token(token)
    if user is None:
        await websocket.send_text("ERROR: invalid token")
        await websocket.close(code=4401)
        return
    if user.role not in {"user", "poweruser", "admin"}:
        await websocket.send_text("ERROR: insufficient permissions")
        await websocket.close(code=4403)
        return
    if LOCAL_DEV_MODE:
        try:
            resolve_results_owner(task_name, user)
        except HTTPException as exc:
            await websocket.send_text(f"ERROR: {exc.detail}")
            await websocket.close(code=4404)
            return
        await websocket.send_text(f"local-dev exec connected to {task_name} with {command}")
        try:
            while True:
                data = await websocket.receive_text()
                await websocket.send_text(f"echo: {data}")
        except WebSocketDisconnect:
            return
    try:
        namespace, pod_name = resolve_task_pod(task_name, user)
    except HTTPException as e:
        await websocket.send_text(f"ERROR: {e.detail}")
        await websocket.close(code=4404)
        return

    load_kubernetes_config()
    v1 = kubernetes.client.CoreV1Api()
    exec_stream = k8s_stream(
        v1.connect_get_namespaced_pod_exec, pod_name, namespace,
        command=[command], container="main",
        stderr=True, stdin=True, stdout=True, tty=False,
        _preload_content=False,
    )
    loop = asyncio.get_event_loop()

    def pump_output():
        """Blokujące pompowanie wyjścia ze strumienia exec; uruchamiane w wątku."""
        chunks = []
        try:
            exec_stream.update(timeout=1)
            if exec_stream.peek_stdout():
                chunks.append(exec_stream.read_stdout())
            if exec_stream.peek_stderr():
                chunks.append(exec_stream.read_stderr())
        except Exception:
            pass
        return "".join(chunks), exec_stream.is_open()

    try:
        while True:
            # Pompuj wyjście z poda (blokujące wywołania w executorze, by nie blokować pętli async).
            out, is_open = await loop.run_in_executor(None, pump_output)
            if out:
                await websocket.send_text(out)
            if not is_open:
                break
            # Odbierz stdin od klienta (jeśli jest), bez blokowania.
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=0.05)
                if data:
                    await loop.run_in_executor(None, exec_stream.write_stdin, data)
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_text(f"ERROR: {e}")
        except Exception:
            pass
    finally:
        try:
            exec_stream.close()
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass

def task_from_job(job: JobDB) -> TaskCreate:
    """Build the task contract used by both immediate and background job dispatch."""
    return TaskCreate(
        image=job.image,
        command=json.loads(job.command or "[]"),
        resources=json.loads(job.resources or "{}"),
        time_limit_seconds=job.time_limit_seconds,
        gpu_uuid=job.gpu_uuid,
        vram_limit_gb=job.vram_limit_gb,
        project=job.project,
        gpu_partition=job.gpu_partition,
    )


@app.post("/jobs", dependencies=[Depends(check_role(["user", "poweruser", "admin"]))])
def create_job(job: JobCreate, current_user: UserDB = Depends(get_current_user)):
    audit_note(target=job.display_name or job.image, image=job.image, gpu_uuid=job.gpu_uuid,
               vram_limit_gb=job.vram_limit_gb, project=job.project, priority=job.priority,
               start_time=str(job.start_time), end_time=str(job.end_time))
    ensure_project_access(job.project, current_user)  # członkostwo w projekcie
    start_time = naive_utc(job.start_time)
    end_time = naive_utc(job.end_time)
    if end_time <= start_time:
        raise HTTPException(status_code=400, detail="End time must be after start time")
    # Zaplanowane zadanie zajmuje jedną kartę, którą rezerwuje; limity te same co dla /tasks.
    enforce_quota_for_request(current_user, image=job.image, resources=job.resources,
                              gpus=1, replicas=1, gpu_uuid=job.gpu_uuid,
                              vram_limit_gb=job.vram_limit_gb, project=job.project,
                              gpu_partition=job.gpu_partition)
    db = SessionLocal()
    try:
        reservation = allocate_reservation(
            db,
            ReservationCreate(
                gpu_uuid=job.gpu_uuid,
                start_time=start_time,
                end_time=end_time,
                priority=job.priority,
                gpu_partition=job.gpu_partition,
            ),
            current_user,
        )
        db.flush()

        time_limit_seconds = max(1, int((end_time - start_time).total_seconds()))
        record = JobDB(
            reservation_id=reservation.id,
            user_id=current_user.id,
            username=current_user.username,
            gpu_uuid=job.gpu_uuid,
            start_time=start_time,
            end_time=end_time,
            image=job.image,
            command=json.dumps(job.command),
            resources=json.dumps(job.resources or {}),
            time_limit_seconds=time_limit_seconds,
            vram_limit_gb=job.vram_limit_gb,
            project=job.project,
            gpu_partition=job.gpu_partition,
            priority=job.priority,
            status="scheduled",
            display_name=job.display_name,
        )
        db.add(record)
        db.commit()
        db.refresh(record)

        if start_time <= datetime.utcnow():
            dispatched = dispatch_task_for_user(current_user.username, task_from_job(record))
            record.task_name = dispatched.get("task")
            record.status = "submitted"
            db.commit()
            db.refresh(record)

        result = serialize_job_record(record)
        db.close()
        return {"message": "Job scheduled", "job": result}
    except HTTPException:
        db.rollback()
        db.close()
        raise
    except Exception as e:
        db.rollback()
        db.close()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/jobs", dependencies=[Depends(check_role(["user", "poweruser", "admin", "readonly"]))])
def list_jobs(current_user: UserDB = Depends(get_current_user)):
    db = SessionLocal()
    query = db.query(JobDB)
    if current_user.role != "admin":
        query = query.filter(JobDB.username == current_user.username)
    jobs = query.order_by(JobDB.start_time.desc()).all()
    outcomes = load_task_outcomes([job.task_name for job in jobs if job.task_name])
    result = [serialize_job_record(job, outcomes) for job in jobs]
    db.close()
    return {"jobs": result}

@app.delete("/jobs/{job_id}", dependencies=[Depends(check_role(["user", "poweruser", "admin"]))])
def cancel_job(job_id: int, current_user: UserDB = Depends(get_current_user)):
    db = SessionLocal()
    query = db.query(JobDB).filter(JobDB.id == job_id)
    if current_user.role != "admin":
        query = query.filter(JobDB.username == current_user.username)
    job = query.first()
    if not job:
        db.close()
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status == "scheduled":
        job.status = "cancelled"
        reservation = db.query(Reservation).filter(Reservation.id == job.reservation_id).first()
        if reservation:
            reservation.status = "cancelled"
        db.commit()
    db.close()
    return {"message": "Job cancelled", "job": job_id}

# ---------- Kolejka zadań (priorytety, fair-share, zależności, multi-GPU/node) ----------
@app.post("/queue", dependencies=[Depends(check_role(["user", "poweruser", "admin"]))])
def queue_submit(req: QueueSubmit, current_user: UserDB = Depends(get_current_user)):
    """Add a workload to the priority queue with dependency and parallel-execution settings."""
    audit_note(target=req.display_name or req.image, image=req.image, gpus=req.gpus, replicas=req.replicas,
               vram_limit_gb=req.vram_limit_gb, project=req.project, priority=req.priority, gpu_uuid=req.gpu_uuid)
    ensure_schedulable_gpu_uuid(req.gpu_uuid)
    ensure_project_access(req.project, current_user)  # członkostwo w projekcie
    enforce_quota_for_request(current_user, image=req.image, resources=req.resources,
                              gpus=req.gpus, replicas=req.replicas, gpu_uuid=req.gpu_uuid,
                              vram_limit_gb=req.vram_limit_gb, project=req.project)
    db = SessionLocal()
    try:
        # Walidacja zależności: muszą istnieć i należeć do tego samego użytkownika (lub admin).
        for dep_id in req.depends_on:
            dep = db.query(QueuedJobDB).filter(QueuedJobDB.id == dep_id).first()
            if not dep:
                raise HTTPException(status_code=400, detail=f"Dependency {dep_id} not found")
        initial_status = "waiting_deps" if req.depends_on else "queued"
        q = QueuedJobDB(
            user=current_user.username,
            team=current_user.team,
            image=req.image,
            command=json.dumps(req.command),
            resources=json.dumps(req.resources or {}),
            gpu_uuid=req.gpu_uuid,
            gpus=req.gpus,
            replicas=req.replicas,
            gang=req.gang,
            mpi=req.mpi,
            vram_limit_gb=req.vram_limit_gb,
            project=req.project,
            priority=req.priority,
            time_limit_seconds=req.time_limit_seconds,
            depends_on=json.dumps(req.depends_on),
            status=initial_status,
            display_name=req.display_name,
        )
        db.add(q)
        db.commit()
        db.refresh(q)
        result = serialize_queued_job(q)
        return {"message": "Queued", "job": result}
    except HTTPException:
        db.rollback()
        raise
    finally:
        db.close()

@app.get("/queue", dependencies=[Depends(check_role(["user", "poweruser", "admin", "readonly"]))])
def queue_list(current_user: UserDB = Depends(get_current_user)):
    """Podgląd kolejki (dla WebGUI). Posortowane wg priorytetu i czasu zgłoszenia."""
    db = SessionLocal()
    query = db.query(QueuedJobDB)
    if current_user.role != "admin":
        query = query.filter(QueuedJobDB.user == current_user.username)
    items = query.order_by(QueuedJobDB.priority.desc(), QueuedJobDB.created_at).all()
    outcomes = load_task_outcomes(
        [name for q in items for name in json.loads(q.task_names or "[]")]
    )
    result = [serialize_queued_job(q, outcomes) for q in items]
    db.close()
    return {"queue": result}

@app.delete("/queue/{queue_id}", dependencies=[Depends(check_role(["user", "poweruser", "admin"]))])
def queue_cancel(queue_id: int, current_user: UserDB = Depends(get_current_user)):
    db = SessionLocal()
    try:
        q = db.query(QueuedJobDB).filter(QueuedJobDB.id == queue_id).first()
        if not q:
            raise HTTPException(status_code=404, detail="Queued job not found")
        if current_user.role != "admin" and q.user != current_user.username:
            raise HTTPException(status_code=403, detail="Not your job")
        if q.status in ("queued", "waiting_deps"):
            q.status = "cancelled"
            db.commit()
        return {"message": "Queued job cancelled", "id": queue_id}
    except HTTPException:
        db.rollback()
        raise
    finally:
        db.close()

def gpu_uuid_to_user_map():
    """Map GPU UUIDs to users from task Pods in user namespaces."""
    mapping = {}
    try:
        load_kubernetes_config()
        v1 = kubernetes.client.CoreV1Api()
        pods = v1.list_pod_for_all_namespaces(label_selector="app=task")
        for pod in pods.items:
            labels = pod.metadata.labels or {}
            user = labels.get("user")
            ns = pod.metadata.namespace or ""
            if not user and ns.startswith("user-"):
                user = ns[len("user-"):]
            if not user:
                continue
            node = pod.spec.node_name
            if node:
                mapping.setdefault(node, user)
    except Exception as exc:
        logger.debug("Could not build GPU->user map: %s", exc)
    return mapping

def dcgm_instance_to_node_map():
    """Mapuje adres z etykiety `instance` metryk DCGM (IP poda eksportera) na nazwę węzła.

    dcgm-exporter z GPU Operatora NIE ustawia etykiety `Hostname`, a `instance` to `IP:port`
    poda eksportera (np. "10.42.0.158:9400"). Dotychczasowe `node_user.get(node)` porównywało
    to z nazwą węzła (np. "gigussmall"), więc nigdy nie trafiało i KAŻDY wiersz gpu_metrics
    zapisywał się jako 'unassigned'. Efekt: /analytics/usage i /gpu/usage per użytkownik były
    trwale puste, mimo poprawnie zbieranych metryk.
    """
    out = {}
    try:
        load_kubernetes_config()
        v1 = kubernetes.client.CoreV1Api()
        pods = v1.list_pod_for_all_namespaces(label_selector="app=nvidia-dcgm-exporter")
        for pod in pods.items:
            ip = (pod.status.pod_ip or "").strip()
            node = pod.spec.node_name if pod.spec else None
            if ip and node:
                out[ip] = node
    except Exception as exc:
        logger.debug("Could not map dcgm-exporter instances to nodes: %s", exc)
    return out


def resolve_metric_node(labels: dict, node_user: dict, inst_node: dict) -> str:
    """Ustala nazwę węzła dla serii metryk DCGM (Hostname -> instance IP -> surowa wartość)."""
    raw = labels.get("Hostname") or labels.get("instance", "") or ""
    if raw and raw in node_user:
        return raw
    host = raw.split(":")[0]
    return inst_node.get(host) or raw


def query_prometheus(promql: str):
    """Zwraca listę (metric_labels, value) z Prometheusa dla zapytania instant."""
    try:
        resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": promql}, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success":
            return []
        return [(item.get("metric", {}), float(item["value"][1])) for item in data.get("data", {}).get("result", [])]
    except Exception as exc:
        logger.debug("Prometheus query failed (%s): %s", promql, exc)
        return []

def collect_gpu_metrics_once():
    """Read DCGM metrics from Prometheus and store per-user GPU metric rows."""
    util = query_prometheus("DCGM_FI_DEV_GPU_UTIL")
    if not util:
        logger.debug("No DCGM utilization metrics available yet")
        return 0
    mem = {m.get("gpu", m.get("UUID", "")): v for m, v in query_prometheus("DCGM_FI_DEV_FB_USED")}
    temp = {m.get("gpu", m.get("UUID", "")): v for m, v in query_prometheus("DCGM_FI_DEV_GPU_TEMP")}
    power = {m.get("gpu", m.get("UUID", "")): v for m, v in query_prometheus("DCGM_FI_DEV_POWER_USAGE")}
    node_user = gpu_uuid_to_user_map()
    inst_node = dcgm_instance_to_node_map()
    db = SessionLocal()
    written = 0
    try:
        for labels, utilization in util:
            gpu_id = labels.get("UUID") or labels.get("gpu") or "unknown"
            gpu_key = labels.get("gpu", gpu_id)
            node = resolve_metric_node(labels, node_user, inst_node)
            user = node_user.get(node, "unassigned")
            db.add(GPUMetric(
                user=user,
                gpu_uuid=gpu_id,
                utilization=utilization,
                memory_used=mem.get(gpu_key, 0.0),
                temperature=temp.get(gpu_key, 0.0),
                power=power.get(gpu_key, 0.0),
            ))
            written += 1
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("Failed to write GPU metrics: %s", exc)
    finally:
        db.close()
    return written

def gpu_metrics_loop():
    while True:
        try:
            count = collect_gpu_metrics_once()
            if count:
                logger.info("Collected %s GPU metric rows", count)
        except Exception as exc:
            logger.error("GPU metrics loop error: %s", exc)
        time.sleep(GPU_METRICS_INTERVAL)

def delete_task_cr(username: str, task_name: str):
    """Usuwa Task CR (kontroler skasuje powiązany pod w handlerze on.delete)."""
    try:
        load_kubernetes_config()
        custom_api = kubernetes.client.CustomObjectsApi()
        custom_api.delete_namespaced_custom_object(
            group="devops.local", version="v1", namespace=f"user-{username}",
            plural="tasks", name=task_name)
        logger.info("Reaper deleted task %s for user %s", task_name, username)
        return True
    except kubernetes.client.exceptions.ApiException as e:
        if e.status == 404:
            return True
        logger.warning("Reaper could not delete task %s: %s", task_name, e)
        return False

def cleanup_loop():
    """Reaper: kasuje zadania jobów po końcu rezerwacji oraz samodzielne zadania po TTL."""
    while True:
        try:
            now = datetime.utcnow()
            db = SessionLocal()
            try:
                # 1) Zadania powiązane z jobem: usuń po zakończeniu rezerwacji (end_time).
                expired_jobs = db.query(JobDB).filter(
                    JobDB.status == "submitted",
                    JobDB.end_time <= now,
                ).all()
                for job in expired_jobs:
                    if job.task_name:
                        delete_task_cr(job.username, job.task_name)
                    job.status = "completed"
                    reservation = db.query(Reservation).filter(Reservation.id == job.reservation_id).first()
                    if reservation and reservation.status == "active":
                        reservation.status = "completed"
                db.commit()

                # 2) Samodzielne zadania (bez joba): usuń po TTL (domyślnie 1 dzień).
                if not LOCAL_DEV_MODE:
                    job_task_names = {j.task_name for j in db.query(JobDB).all() if j.task_name}
                    try:
                        load_kubernetes_config()
                        custom_api = kubernetes.client.CustomObjectsApi()
                        objects = custom_api.list_cluster_custom_object(group="devops.local", version="v1", plural="tasks")
                        ttl = timedelta(seconds=CLEANUP_SETTINGS["standalone_task_ttl_seconds"])
                        for item in objects.get("items", []):
                            meta = item.get("metadata", {})
                            name = meta.get("name")
                            ns = meta.get("namespace", "")
                            created = meta.get("creationTimestamp")
                            if not name or name in job_task_names or not created:
                                continue
                            created_dt = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ")
                            if now - created_dt > ttl:
                                username = ns[len("user-"):] if ns.startswith("user-") else item.get("spec", {}).get("user", "")
                                delete_task_cr(username, name)
                    except Exception as exc:
                        logger.error("Reaper standalone-task sweep error: %s", exc)

                # 3) Sprzątanie podów zadań: usuń pody zakończone (Completed/Failed)
                #    oraz osierocone (bez istniejącego Task CR). Samonaprawiające się.
                if not LOCAL_DEV_MODE:
                    try:
                        load_kubernetes_config()
                        v1 = kubernetes.client.CoreV1Api()
                        custom_api = kubernetes.client.CustomObjectsApi()
                        objects = custom_api.list_cluster_custom_object(group="devops.local", version="v1", plural="tasks")
                        live_task_names = {it.get("metadata", {}).get("name") for it in objects.get("items", [])}
                        pods = v1.list_pod_for_all_namespaces(label_selector="app=task")
                        for pod in pods.items:
                            phase = pod.status.phase
                            task_name = (pod.metadata.labels or {}).get("task-name")
                            pod_ns = pod.metadata.namespace
                            pod_name = pod.metadata.name
                            terminal = phase in ("Succeeded", "Failed")
                            orphaned = task_name is not None and task_name not in live_task_names
                            if terminal or orphaned:
                                # Ostatnia chwila, żeby zapisać przyczynę i logi — po
                                # usunięciu poda Kubernetes nie pamięta już nic.
                                record_task_outcome(v1, pod)
                                try:
                                    v1.delete_namespaced_pod(pod_name, pod_ns)
                                    logger.info("Reaper deleted task pod %s/%s (phase=%s, orphaned=%s)",
                                                pod_ns, pod_name, phase, orphaned)
                                except kubernetes.client.exceptions.ApiException as e:
                                    if e.status != 404:
                                        logger.warning("Reaper could not delete pod %s/%s: %s", pod_ns, pod_name, e)
                    except Exception as exc:
                        logger.error("Reaper task-pod sweep error: %s", exc)

                # 4) Wyniki zadań: usuń katalogi wyników starsze niż result_ttl_seconds (na PVC scratch).
                if not LOCAL_DEV_MODE:
                    try:
                        ttl_min = max(1, int(CLEANUP_SETTINGS.get("result_ttl_seconds", 7 * 24 * 3600)) // 60)
                        load_kubernetes_config()
                        v1 = kubernetes.client.CoreV1Api()
                        helpers = v1.list_pod_for_all_namespaces(label_selector="app=results-helper")
                        idle_ttl = max(60, int(CLEANUP_SETTINGS.get("results_helper_idle_ttl_seconds", 1800)))
                        for pod in helpers.items:
                            ns = pod.metadata.namespace
                            # Martwi pomocnicy (Failed/Succeeded/Error) są bezużyteczni —
                            # kasujemy od razu; odtworzą się przy następnym żądaniu wyników.
                            if pod.status.phase != "Running":
                                try:
                                    v1.delete_namespaced_pod(pod.metadata.name, ns)
                                    logger.info("Reaped dead results-helper in %s (phase=%s)",
                                                ns, pod.status.phase)
                                except Exception as exc:
                                    logger.debug("Could not delete dead helper in %s: %s", ns, exc)
                                continue
                            # Usuń podkatalogi wyników starsze niż TTL (mmin = wiek w minutach).
                            try:
                                helper_exec(ns, pod.metadata.name, [
                                    "sh", "-c",
                                    f"find {RESULTS_DIR} -mindepth 1 -maxdepth 1 -type d -mmin +{ttl_min} -exec rm -rf {{}} + 2>/dev/null || true"
                                ])
                            except Exception as exc:
                                logger.debug("Result TTL sweep failed in %s: %s", ns, exc)
                            # Reap bezczynnych pomocników: jeśli ostatnie użycie (lub start,
                            # gdy brak adnotacji) jest starsze niż idle TTL, usuwamy pod.
                            # Odtworzy się przy następnym żądaniu wyników.
                            try:
                                anns = pod.metadata.annotations or {}
                                stamp = anns.get(HELPER_LAST_USED_ANNOTATION)
                                if stamp:
                                    last = datetime.fromisoformat(stamp)
                                else:
                                    last = (pod.status.start_time.replace(tzinfo=None)
                                            if pod.status.start_time else now)
                                if (now - last) > timedelta(seconds=idle_ttl):
                                    v1.delete_namespaced_pod(pod.metadata.name, ns)
                                    logger.info("Reaped idle results-helper in %s (idle %ss)",
                                                ns, int((now - last).total_seconds()))
                            except Exception as exc:
                                logger.debug("Idle results-helper reap failed in %s: %s", ns, exc)
                    except Exception as exc:
                        logger.error("Reaper result-TTL sweep error: %s", exc)
            finally:
                db.close()
            purge_old_audit_entries()
        except Exception as exc:
            logger.error("Cleanup loop error: %s", exc)
        # Czytamy interwał na żywo, aby zmiany przez API działały bez restartu.
        time.sleep(max(5, int(CLEANUP_SETTINGS["cleanup_interval_seconds"])))

def cluster_free_gpus():
    """Szacuje liczbę wolnych GPU w klastrze (pojemność - zaalokowane przez pody zadań)."""
    try:
        load_kubernetes_config()
        v1 = kubernetes.client.CoreV1Api()
        capacity = 0
        for node in v1.list_node().items:
            cap = node.status.allocatable.get("nvidia.com/gpu", "0") if node.status.allocatable else "0"
            capacity += int(cap)
        used = 0
        pods = v1.list_pod_for_all_namespaces(label_selector="app=task")
        for pod in pods.items:
            if pod.status.phase not in ("Pending", "Running"):
                continue
            for c in pod.spec.containers:
                lim = (c.resources.limits or {}) if c.resources else {}
                used += int(lim.get("nvidia.com/gpu", 0) or 0)
        return max(0, capacity - used)
    except Exception as exc:
        logger.debug("cluster_free_gpus error: %s", exc)
        return 0

def recent_usage_by_user(db):
    """Fair-share: sumaryczne GPU aktualnie używane per użytkownik (running w kolejce)."""
    usage = {}
    for q in db.query(QueuedJobDB).filter(QueuedJobDB.status == "running").all():
        usage[q.user] = usage.get(q.user, 0) + (q.gpus or 0) * (q.replicas or 1)
    return usage

def queue_loop():
    """Schedule queued workloads using priority, fair-share, dependencies, gang scheduling, and backfill."""
    while True:
        try:
            db = SessionLocal()
            try:
                now = datetime.utcnow()
                # 1) Odblokuj zadania, których zależności są spełnione (completed).
                waiting = db.query(QueuedJobDB).filter(QueuedJobDB.status == "waiting_deps").all()
                for q in waiting:
                    deps = json.loads(q.depends_on or "[]")
                    if not deps:
                        q.status = "queued"
                        continue
                    dep_rows = db.query(QueuedJobDB).filter(QueuedJobDB.id.in_(deps)).all()
                    statuses = {d.id: d.status for d in dep_rows}
                    if any(statuses.get(d) == "failed" or statuses.get(d) == "cancelled" for d in deps):
                        q.status = "cancelled"   # zależność nie powiedzie się -> anuluj
                    elif all(statuses.get(d) == "completed" for d in deps):
                        q.status = "queued"
                db.commit()

                # 2) Oznacz running -> completed/failed na podstawie podów zadań.
                if not LOCAL_DEV_MODE:
                    running = db.query(QueuedJobDB).filter(QueuedJobDB.status == "running").all()
                    if running:
                        load_kubernetes_config()
                        v1 = kubernetes.client.CoreV1Api()
                        for q in running:
                            names = json.loads(q.task_names or "[]")
                            if not names:
                                continue
                            phases = []
                            for n in names:
                                try:
                                    pod = v1.read_namespaced_pod(f"task-{n}", f"user-{q.user}")
                                    phases.append(pod.status.phase)
                                    if pod.status.phase in ("Succeeded", "Failed"):
                                        record_task_outcome(v1, pod)
                                except kubernetes.client.exceptions.ApiException:
                                    # Poda już nie ma. "Gone" NIE znaczy "sukces": reaper
                                    # kasuje pody Failed i Succeeded na własnym interwale,
                                    # więc nieudane zadanie sprzątnięte przed tym odczytem
                                    # było raportowane jako completed. Rozstrzyga zapisany
                                    # wynik, a nie to, kto wygrał wyścig.
                                    recorded = get_task_outcome(n)
                                    phases.append((recorded or {}).get("phase") or "Gone")
                            if any(p == "Failed" for p in phases):
                                q.status = "failed"; q.finished_at = now
                            elif all(p in ("Succeeded", "Gone") for p in phases):
                                q.status = "completed"; q.finished_at = now
                        db.commit()

                # 3) Wybierz kandydatów (queued), posortuj wg fair-share i priorytetu.
                free = cluster_free_gpus() if not LOCAL_DEV_MODE else 9999
                usage = recent_usage_by_user(db)
                users = {u.username: u for u in db.query(UserDB).all()}
                candidates = db.query(QueuedJobDB).filter(QueuedJobDB.status == "queued").all()

                def effective_priority(q):
                    base = users.get(q.user).priority if users.get(q.user) else 0
                    # fair-share: im więcej użytkownik już używa, tym niższy efektywny priorytet
                    return (q.priority + base) - usage.get(q.user, 0)

                candidates.sort(key=lambda q: (-effective_priority(q), q.created_at or now))

                # 4) Dispatch z uwzględnieniem dostępnych GPU; backfill — mniejsze zadania mogą wyprzedzić.
                for q in candidates:
                    need = (q.gpus or 0) * (q.replicas or 1)
                    if not LOCAL_DEV_MODE and need > free and need > 0:
                        # Backfill: pomiń duże zadanie, spróbuj kolejne, które się zmieści.
                        continue
                    try:
                        if LOCAL_DEV_MODE:
                            q.task_names = json.dumps([f"local-{q.id}"])
                        else:
                            created = dispatch_parallel_job(q)
                            q.task_names = json.dumps(created)
                        q.status = "running"
                        q.started_at = now
                        free -= need
                        usage[q.user] = usage.get(q.user, 0) + need
                        logger.info("Queue dispatched job %s (%s replicas x %s gpu)", q.id, q.replicas, q.gpus)
                    except Exception as exc:
                        logger.error("Queue dispatch failed for %s: %s", q.id, exc)
                        q.status = "failed"; q.finished_at = now
                db.commit()
            finally:
                db.close()
        except Exception as exc:
            logger.error("Queue loop error: %s", exc)
        time.sleep(max(5, QUEUE_INTERVAL))

def notification_loop():
    """Send e-mail or Slack notifications before reservation start and end times."""
    while True:
        try:
            now = datetime.utcnow()
            lead = timedelta(seconds=NOTIFY_LEAD_SECONDS)
            db = SessionLocal()
            try:
                active = db.query(Reservation).filter(Reservation.status == "active").all()
                users = {u.id: u for u in db.query(UserDB).all()}
                for r in active:
                    user = users.get(r.user_id)
                    # Zbliżający się start
                    if not r.notified_start and r.start_time and now <= r.start_time <= now + lead:
                        notify_user(user, "Reservation starting soon",
                                    f"Your reservation for GPU {r.gpu_uuid} starts at {r.start_time.isoformat()}.")
                        r.notified_start = True
                    # Zbliżający się koniec
                    if not r.notified_end and r.end_time and now <= r.end_time <= now + lead:
                        notify_user(user, "Reservation ending soon",
                                    f"Your reservation for GPU {r.gpu_uuid} ends at {r.end_time.isoformat()}.")
                        r.notified_end = True
                db.commit()
            finally:
                db.close()
        except Exception as exc:
            logger.error("Notification loop error: %s", exc)
        time.sleep(max(15, NOTIFY_INTERVAL))

def image_cleanup_loop():
    """Remove images past their unused TTL and enforce the per-repository version limit."""
    while True:
        try:
            now = datetime.utcnow()
            db = SessionLocal()
            try:
                # Nieużywane: brak uploadu/odświeżenia od IMAGE_UNUSED_TTL_SECONDS.
                ttl = timedelta(seconds=IMAGE_UNUSED_TTL_SECONDS)
                for img in db.query(ImageDB).all():
                    if img.created_at and now - img.created_at > ttl:
                        logger.info("Image cleanup: removing unused %s", img.pull_ref)
                        db.delete(img)
                db.commit()
                # Limit wersji per (user, name): zostaw najnowsze IMAGE_MAX_VERSIONS_PER_REPO.
                from collections import defaultdict
                groups = defaultdict(list)
                for img in db.query(ImageDB).order_by(ImageDB.created_at.desc()).all():
                    groups[(img.user, img.name)].append(img)
                for _, imgs in groups.items():
                    for extra in imgs[IMAGE_MAX_VERSIONS_PER_REPO:]:
                        logger.info("Image cleanup: pruning old version %s", extra.pull_ref)
                        db.delete(extra)
                db.commit()
            finally:
                db.close()
        except Exception as exc:
            logger.error("Image cleanup loop error: %s", exc)
        time.sleep(max(60, IMAGE_CLEANUP_INTERVAL))

def scheduler_loop():
    while True:
        try:
            db = SessionLocal()
            due = db.query(JobDB).filter(JobDB.status == "scheduled", JobDB.start_time <= datetime.utcnow()).all()
            for job in due:
                try:
                    dispatched = dispatch_task_for_user(job.username, task_from_job(job))
                    job.task_name = dispatched.get("task")
                    job.status = "submitted"
                except Exception as exc:
                    logger.error("Failed to dispatch scheduled job %s: %s", job.id, exc)
                    job.status = "dispatch_failed"
            db.commit()
            db.close()
        except Exception as exc:
            logger.error("Scheduled job loop error: %s", exc)
        time.sleep(10)

def bootstrap_admin():
    """Tworzy pierwszego administratora przy starcie, jeśli baza nie ma żadnych użytkowników.

    W pełni automatyczne (no-touch): po deployu skryptami system ma od razu konto admina.
    Idempotentne — nic nie robi, gdy istnieje jakikolwiek użytkownik.
    """
    db = SessionLocal()
    try:
        if db.query(UserDB).count() > 0:
            return
        admin = UserDB(
            username=BOOTSTRAP_ADMIN_USERNAME,
            hashed_password=hash_password(BOOTSTRAP_ADMIN_PASSWORD),
            role="admin",
            quota_cpu="4", quota_memory="8Gi", quota_gpu=4, quota_vram_gb=16,
            disk_home_gb=DEFAULT_DISK_HOME_GB,
            disk_scratch_gb=DEFAULT_DISK_SCRATCH_GB,
            disk_project_gb=DEFAULT_DISK_PROJECT_GB,
        )
        db.add(admin)
        db.commit()
        logger.info("Bootstrapped initial admin user '%s'", BOOTSTRAP_ADMIN_USERNAME)
    except Exception as exc:
        db.rollback()
        logger.error("Admin bootstrap failed: %s", exc)
        db.close()
        return
    finally:
        db.close()
    # Tworzymy namespace + przestrzenie robocze admina (jak przy POST /users).
    try:
        load_kubernetes_config()
        v1 = kubernetes.client.CoreV1Api()
        ns_name = f"user-{BOOTSTRAP_ADMIN_USERNAME}"
        try:
            v1.create_namespace(body={"metadata": {"name": ns_name, "labels": {
                "devops.dev/user": BOOTSTRAP_ADMIN_USERNAME,
                "devops.dev/user-namespace": "true",
            }}})
        except kubernetes.client.exceptions.ApiException as e:
            if e.status != 409:
                raise
        create_user_workspaces(v1, ns_name, BOOTSTRAP_ADMIN_USERNAME, {
            "home": DEFAULT_DISK_HOME_GB,
            "scratch": DEFAULT_DISK_SCRATCH_GB,
            "project": DEFAULT_DISK_PROJECT_GB,
        })
    except Exception as e:
        logger.warning("Could not create admin namespace/workspaces: %s", e)

@app.on_event("startup")
def start_scheduler():
    # Diagnostyka storage PRZED bootstrapem — bootstrap tworzy PVC admina, więc jeśli
    # klasa storage jest zła/nieobecna, chcemy to zobaczyć w logu od razu, a nie dopiero
    # po cichym warningu przy tworzeniu PVC (który zwracał 200 mimo niepowodzenia).
    warn_if_no_default_storage_class()
    # Migracja: napraw PVC z martwą klasą storage (inaczej zadania i wyniki są trwale zepsute
    # dla użytkowników założonych starszą wersją API).
    heal_unsatisfiable_workspace_pvcs()
    # Bootstrap pierwszego admina (no-touch), zanim wystartują wątki tła.
    if BOOTSTRAP_ADMIN:
        bootstrap_admin()
    thread = threading.Thread(target=scheduler_loop, daemon=True)
    thread.start()
    # Start the background GPU metrics collector when enabled.
    if GPU_METRICS_ENABLED:
        metrics_thread = threading.Thread(target=gpu_metrics_loop, daemon=True)
        metrics_thread.start()
    # Reaper: sprzątanie wygasłych zadań/jobów.
    if CLEANUP_ENABLED:
        cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
        cleanup_thread.start()
    # Kolejka zadań (priorytety/fair-share/zależności/gang/backfill).
    if QUEUE_ENABLED:
        threading.Thread(target=queue_loop, daemon=True).start()
    # Powiadomienia o rezerwacjach (e-mail/Slack).
    if NOTIFY_ENABLED:
        threading.Thread(target=notification_loop, daemon=True).start()
    # Automatyczne czyszczenie nieużywanych obrazów.
    if IMAGE_CLEANUP_ENABLED:
        threading.Thread(target=image_cleanup_loop, daemon=True).start()

@app.get("/settings/cleanup", dependencies=[Depends(check_role(["admin"]))])
def get_cleanup_settings():
    """Zwraca aktualne ustawienia sprzątania (TTL zadań, interwał reapera)."""
    return {"enabled": CLEANUP_ENABLED, **CLEANUP_SETTINGS}

@app.put("/settings/cleanup", dependencies=[Depends(check_role(["admin"]))])
def update_cleanup_settings(settings: CleanupSettings):
    """Aktualizuje ustawienia sprzątania w czasie działania (bez restartu). Tylko admin."""
    audit_note(cleanup_interval_seconds=settings.cleanup_interval_seconds,
               standalone_task_ttl_seconds=settings.standalone_task_ttl_seconds,
               result_ttl_seconds=settings.result_ttl_seconds,
               results_helper_idle_ttl_seconds=settings.results_helper_idle_ttl_seconds)
    if settings.cleanup_interval_seconds is not None:
        if settings.cleanup_interval_seconds < 5:
            raise HTTPException(status_code=400, detail="cleanup_interval_seconds must be >= 5")
        CLEANUP_SETTINGS["cleanup_interval_seconds"] = settings.cleanup_interval_seconds
    if settings.standalone_task_ttl_seconds is not None:
        if settings.standalone_task_ttl_seconds < 1:
            raise HTTPException(status_code=400, detail="standalone_task_ttl_seconds must be >= 1")
        CLEANUP_SETTINGS["standalone_task_ttl_seconds"] = settings.standalone_task_ttl_seconds
    if settings.result_ttl_seconds is not None:
        if settings.result_ttl_seconds < 60:
            raise HTTPException(status_code=400, detail="result_ttl_seconds must be >= 60")
        CLEANUP_SETTINGS["result_ttl_seconds"] = settings.result_ttl_seconds
    if settings.results_helper_idle_ttl_seconds is not None:
        if settings.results_helper_idle_ttl_seconds < 60:
            raise HTTPException(status_code=400, detail="results_helper_idle_ttl_seconds must be >= 60")
        CLEANUP_SETTINGS["results_helper_idle_ttl_seconds"] = settings.results_helper_idle_ttl_seconds
    return {"message": "Cleanup settings updated", "enabled": CLEANUP_ENABLED, **CLEANUP_SETTINGS}

@app.get("/gpu/usage/{username}", dependencies=[Depends(check_role(["admin"]))])
def gpu_usage(username: str, hours: int = 24):
    db = SessionLocal()
    since = datetime.utcnow() - timedelta(hours=hours)
    metrics = db.query(GPUMetric).filter(GPUMetric.user == username, GPUMetric.timestamp >= since).all()
    db.close()
    return [{"timestamp": m.timestamp.isoformat(), "utilization": m.utilization, "memory": m.memory_used} for m in metrics]

@app.get("/gpu/usage")
def gpu_usage_me(hours: int = 24, current_user: UserDB = Depends(get_current_user)):
    """Return GPU usage history for the authenticated user."""
    db = SessionLocal()
    since = datetime.utcnow() - timedelta(hours=hours)
    metrics = db.query(GPUMetric).filter(GPUMetric.user == current_user.username, GPUMetric.timestamp >= since).order_by(GPUMetric.timestamp).all()
    db.close()
    return [{
        "timestamp": m.timestamp.isoformat(),
        "gpu_uuid": m.gpu_uuid,
        "utilization": m.utilization,
        "memory_used": m.memory_used,
        "temperature": m.temperature,
        "power": m.power,
    } for m in metrics]

# ---------- Reservation calendar export and import (iCal) ----------
def _ical_dt(dt: datetime):
    return dt.strftime("%Y%m%dT%H%M%SZ")

@app.get("/reservations/calendar.ics")
def reservations_ical(token: Optional[str] = None):
    """Export reservations as an iCal feed, optionally scoped by a JWT query parameter."""
    db = SessionLocal()
    query = db.query(Reservation).filter(Reservation.status == "active")
    # Opcjonalne zawężenie do użytkownika, gdy podano token.
    user = ws_user_from_token(token) if token else None
    if user and user.role != "admin":
        query = query.filter(Reservation.user_id == user.id)
    reservations = query.all()
    users = {u.id: u.username for u in db.query(UserDB).all()}
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//MLManage//Reservations//EN"]
    for r in reservations:
        lines += [
            "BEGIN:VEVENT",
            f"UID:reservation-{r.id}@mlmanage",
            f"DTSTART:{_ical_dt(r.start_time)}",
            f"DTEND:{_ical_dt(r.end_time)}",
            f"SUMMARY:GPU {r.gpu_uuid} — {users.get(r.user_id, r.user_id)}",
            f"DESCRIPTION:Priority {r.priority}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    db.close()
    return StreamingResponse(iter(["\r\n".join(lines)]), media_type="text/calendar",
                             headers={"Content-Disposition": "attachment; filename=reservations.ics"})

@app.post("/reservations/import.ics", dependencies=[Depends(check_role(["user", "poweruser", "admin"]))])
async def reservations_import_ical(file: UploadFile = File(...), current_user: UserDB = Depends(get_current_user)):
    """Import reservations from VEVENT entries in an uploaded iCal file."""
    raw = (await file.read()).decode("utf-8", errors="replace")
    db = SessionLocal()
    created = 0
    try:
        cur = {}
        for line in raw.splitlines():
            line = line.strip()
            if line == "BEGIN:VEVENT":
                cur = {}
            elif line.startswith("DTSTART"):
                cur["start"] = datetime.strptime(line.split(":", 1)[1].strip(), "%Y%m%dT%H%M%SZ")
            elif line.startswith("DTEND"):
                cur["end"] = datetime.strptime(line.split(":", 1)[1].strip(), "%Y%m%dT%H%M%SZ")
            elif line.startswith("SUMMARY"):
                m = re.search(r"GPU\s+(\S+)", line)
                cur["gpu"] = m.group(1) if m else "gpu-imported"
            elif line == "END:VEVENT":
                if "start" in cur and "end" in cur:
                    conflict = db.query(Reservation).filter(
                        Reservation.gpu_uuid == cur.get("gpu", "gpu-imported"),
                        Reservation.status == "active",
                        Reservation.start_time < cur["end"],
                        Reservation.end_time > cur["start"],
                    ).first()
                    if not conflict:
                        db.add(Reservation(user_id=current_user.id, gpu_uuid=cur.get("gpu", "gpu-imported"),
                                           start_time=cur["start"], end_time=cur["end"]))
                        created += 1
        db.commit()
    finally:
        db.close()
    return {"message": "Imported", "created": created}

# ---------- Resource-efficiency, energy, and carbon analysis ----------
@app.get("/analytics/usage")
def analytics_usage(hours: int = 24, group_by: str = "user", subject: Optional[str] = None,
                    current_user: UserDB = Depends(get_current_user)):
    """Return utilization, energy, and carbon estimates grouped by user, project, or team.

    Energy is calculated from sampled GPU power; carbon is energy multiplied by `CO2_KG_PER_KWH`.
    """
    # Non-admin users may query only their own user, team, or authorized project usage.
    if subject and current_user.role != "admin":
        if group_by == "project":
            ensure_project_access(subject, current_user)   # 403, gdy nie jest członkiem
        elif group_by == "team":
            if subject != (current_user.team or ""):
                raise HTTPException(status_code=403, detail="You may only query your own team's usage")
        elif subject != current_user.username:
            raise HTTPException(status_code=403, detail="You may only query your own usage")
    db = SessionLocal()
    since = datetime.utcnow() - timedelta(hours=hours)
    q = db.query(GPUMetric).filter(GPUMetric.timestamp >= since)
    if current_user.role != "admin":
        # Non-admins only ever see their own telemetry, regardless of subject.
        q = q.filter(GPUMetric.user == current_user.username)
    elif subject:
        # Admins may scope to a specific subject (user/team). GPUMetric has no project column.
        if group_by == "team":
            members = [u.username for u in db.query(UserDB).filter(UserDB.team == subject).all()]
            # An empty team must yield no rows, not all rows: match an impossible username.
            q = q.filter(GPUMetric.user.in_(members if members else ["\x00__no_such_user__"]))
        else:
            q = q.filter(GPUMetric.user == subject)
    rows = q.all()
    users = {u.username: u for u in db.query(UserDB).all()}
    # interwał próbkowania w godzinach (do energii)
    sample_h = GPU_METRICS_INTERVAL / 3600.0
    agg = {}
    for m in rows:
        if group_by == "team":
            key = (users.get(m.user).team if users.get(m.user) and users.get(m.user).team else "no-team")
        elif group_by == "project":
            key = "all"  # projekt nie jest w GPUMetric; grupujemy zbiorczo (rozszerzalne)
        else:
            key = m.user
        a = agg.setdefault(key, {"samples": 0, "util_sum": 0.0, "energy_kwh": 0.0})
        a["samples"] += 1
        a["util_sum"] += (m.utilization or 0.0)
        a["energy_kwh"] += (m.power or 0.0) / 1000.0 * sample_h
    result = []
    for key, a in agg.items():
        avg_util = (a["util_sum"] / a["samples"]) if a["samples"] else 0.0
        result.append({
            "group": key,
            "avg_utilization_percent": round(avg_util, 1),
            "energy_kwh": round(a["energy_kwh"], 3),
            "co2_kg": round(a["energy_kwh"] * CO2_KG_PER_KWH, 3),
            "efficiency_note": "low utilization" if avg_util < 30 else "ok",
        })
    db.close()
    return {"group_by": group_by, "hours": hours, "results": result}

@app.get("/analytics/activity")
def analytics_activity(hours: int = 168, group_by: str = "user", subject: Optional[str] = None,
                       current_user: UserDB = Depends(get_current_user)):
    """Lista aktywności (workloadów) w oknie czasu — dla widoku Usage we frontendzie.

    Źródłem są joby (rezerwacja + zadanie). Non-admin widzi tylko własne; admin może
    zawęzić przez `subject` zależnie od `group_by` (user/team/project).
    """
    since = datetime.utcnow() - timedelta(hours=hours)
    db = SessionLocal()
    try:
        q = db.query(JobDB).filter(JobDB.start_time >= since)
        if current_user.role != "admin":
            q = q.filter(JobDB.username == current_user.username)
        elif subject:
            if group_by == "project":
                q = q.filter(JobDB.project == subject)
            elif group_by == "team":
                members = [u.username for u in db.query(UserDB).filter(UserDB.team == subject).all()]
                q = q.filter(JobDB.username.in_(members if members else ["\x00__no_such_user__"]))
            else:
                q = q.filter(JobDB.username == subject)
        jobs = q.order_by(JobDB.start_time.desc()).all()

        terminal = {"completed", "failed", "cancelled", "preempted", "expired"}
        activity = []
        for job in jobs:
            start = job.start_time
            end = job.end_time
            duration = int((end - start).total_seconds()) if (start and end) else 0
            if duration < 0:
                duration = 0
            try:
                res = json.loads(job.resources or "{}")
                gpu_count = int((res.get("limits", {}) or {}).get("nvidia.com/gpu", 0))
            except Exception:
                gpu_count = 0
            if gpu_count == 0 and job.gpu_uuid:
                gpu_count = 1
            activity.append({
                "scope": group_by,
                "job_id": str(job.id),
                "job_name": job.task_name or f"job-{job.id}",
                "user": job.username,
                "project": job.project,
                "status": job.status,
                "gpu_count": gpu_count,
                "gpu_uuid": job.gpu_uuid,
                "started_at": start.isoformat() if start else None,
                "finished_at": end.isoformat() if (end and job.status in terminal) else None,
                "duration_seconds": duration,
                "allocated_gpu_seconds": duration * gpu_count,
                "source": "workload",
            })
        return activity
    finally:
        db.close()

# ---------- Real-time workspace storage usage ----------
@app.get("/disk/usage")
def disk_usage(current_user: UserDB = Depends(get_current_user)):
    """Return current PVC usage for namespaces visible to the authenticated user."""
    def parse_storage_string(size_str: str) -> int:
        """Parse Kubernetes storage string (e.g., '10Gi', '5G', '100Mi') to bytes."""
        if not size_str:
            return 0
        size_str = size_str.strip()
        multipliers = {
            'Ki': 1024,
            'Mi': 1024 ** 2,
            'Gi': 1024 ** 3,
            'Ti': 1024 ** 4,
            'K': 1000,
            'M': 1000 ** 2,
            'G': 1000 ** 3,
            'T': 1000 ** 4,
        }
        for suffix, mult in multipliers.items():
            if size_str.endswith(suffix):
                try:
                    return int(float(size_str[:-len(suffix)]) * mult)
                except ValueError:
                    return 0
        try:
            return int(float(size_str))
        except ValueError:
            return 0

    if LOCAL_DEV_MODE:
        # Mock data for local development with realistic usage numbers
        home_capacity = (current_user.disk_home_gb or DEFAULT_DISK_HOME_GB) * (1024 ** 3)
        scratch_capacity = (current_user.disk_scratch_gb or DEFAULT_DISK_SCRATCH_GB) * (1024 ** 3)
        project_capacity = (current_user.disk_project_gb or DEFAULT_DISK_PROJECT_GB) * (1024 ** 3)
        return {"volumes": [
            {
                "namespace": f"user-{current_user.username}",
                "pvc": f"{current_user.username}-home",
                "volume_type": "home",
                "requested": f"{current_user.disk_home_gb or DEFAULT_DISK_HOME_GB}Gi",
                "requested_gb": current_user.disk_home_gb or DEFAULT_DISK_HOME_GB,
                "capacity_bytes": home_capacity,
                "used_bytes": int(home_capacity * 0.45),  # Mock: 45% used
                "usage_percent": 45,
                "phase": "Bound",
            },
            {
                "namespace": f"user-{current_user.username}",
                "pvc": f"{current_user.username}-scratch",
                "volume_type": "scratch",
                "requested": f"{current_user.disk_scratch_gb or DEFAULT_DISK_SCRATCH_GB}Gi",
                "requested_gb": current_user.disk_scratch_gb or DEFAULT_DISK_SCRATCH_GB,
                "capacity_bytes": scratch_capacity,
                "used_bytes": int(scratch_capacity * 0.32),  # Mock: 32% used
                "usage_percent": 32,
                "phase": "Bound",
            },
            {
                "namespace": f"user-{current_user.username}",
                "pvc": f"{current_user.username}-project",
                "volume_type": "project",
                "requested": f"{current_user.disk_project_gb or DEFAULT_DISK_PROJECT_GB}Gi",
                "requested_gb": current_user.disk_project_gb or DEFAULT_DISK_PROJECT_GB,
                "capacity_bytes": project_capacity,
                "used_bytes": int(project_capacity * 0.68),  # Mock: 68% used
                "usage_percent": 68,
                "phase": "Bound",
            },
        ]}
    load_kubernetes_config()
    v1 = kubernetes.client.CoreV1Api()
    result = []
    if current_user.role == "admin":
        namespaces = [ns.metadata.name for ns in v1.list_namespace(label_selector="devops.dev/user-namespace=true").items]
    else:
        namespaces = [f"user-{current_user.username}"]
    for ns in namespaces:
        try:
            pvcs = v1.list_namespaced_persistent_volume_claim(ns)
        except kubernetes.client.exceptions.ApiException:
            continue
        for pvc in pvcs.items:
            vol_type = "unknown"
            for t in ("home", "scratch", "project"):
                if t in pvc.metadata.name:
                    vol_type = t
            requested = (pvc.spec.resources.requests or {}).get("storage") if pvc.spec.resources else None
            requested_bytes = parse_storage_string(requested) if requested else 0

            # Initialize usage fields as null (optional for backward compatibility)
            used_bytes = None
            capacity_bytes = None
            usage_percent = None

            # Try to get real-time usage from PV status
            if pvc.status and pvc.status.capacity:
                try:
                    capacity_bytes = parse_storage_string(pvc.status.capacity.get('storage', '0'))
                except Exception:
                    pass

            # Try to fetch real-time usage from the PV
            try:
                if pvc.spec.volume_name:
                    pv = v1.read_persistent_volume(pvc.spec.volume_name)
                    if pv.status and pv.status.capacity:
                        capacity_bytes = parse_storage_string(pv.status.capacity.get('storage', '0'))
                    # Try to get usage from PV status conditions or annotations
                    if pv.metadata and pv.metadata.annotations:
                        if 'disk.usage.bytes' in pv.metadata.annotations:
                            try:
                                used_bytes = int(pv.metadata.annotations['disk.usage.bytes'])
                            except (ValueError, KeyError):
                                pass
            except Exception:
                pass

            # Try to fetch usage metrics from a pod that uses this PVC
            # This method uses 'df' command inside a running pod
            if used_bytes is None and capacity_bytes is None:
                try:
                    pods = v1.list_namespaced_pod(ns, label_selector=f"pvc={pvc.metadata.name}")
                    if not pods.items and vol_type != "unknown":
                        # Try to find pods mounted to this volume by volume type
                        pods = v1.list_namespaced_pod(ns, label_selector=f"app={vol_type}")

                    if pods.items:
                        pod = pods.items[0]
                        container_name = pod.spec.containers[0].name if pod.spec.containers else None
                        if container_name:
                            # Execute 'df' inside the pod to get usage
                            try:
                                exec_command = [
                                    '/bin/sh', '-c',
                                    f'df -B1 /{vol_type} 2>/dev/null || df -B1 /mnt 2>/dev/null || echo ""'
                                ]
                                resp = k8s_stream(
                                    v1.connect_get_namespaced_pod_exec,
                                    pod.metadata.name, ns,
                                    command=exec_command,
                                    stderr=True, stdin=False,
                                    stdout=True, tty=False
                                )
                                # Parse df output: "Filesystem 1B-blocks Used Available Use% Mounted on"
                                lines = resp.strip().split('\n')
                                if len(lines) > 1:
                                    parts = lines[1].split()
                                    if len(parts) >= 3:
                                        try:
                                            capacity_bytes = int(parts[1])
                                            used_bytes = int(parts[2])
                                        except (ValueError, IndexError):
                                            pass
                            except Exception:
                                pass
                except Exception:
                    pass

            # Calculate usage percentage if we have capacity
            if capacity_bytes and capacity_bytes > 0:
                if used_bytes is not None:
                    usage_percent = (used_bytes / capacity_bytes) * 100
                else:
                    # Fallback: use requested size as an estimate
                    if requested_bytes > 0:
                        usage_percent = min(50, (requested_bytes / capacity_bytes) * 100)
                    else:
                        # Default conservative estimate
                        usage_percent = 0

            result.append({
                "namespace": ns,
                "name": pvc.metadata.name,
                "pvc": pvc.metadata.name,
                "volume_type": vol_type,
                "type": vol_type,
                "requested": requested,
                "requested_gb": requested_bytes / (1024 ** 3) if requested_bytes > 0 else None,
                "capacity_bytes": capacity_bytes,
                "used_bytes": used_bytes,
                "usage_percent": usage_percent,
                "phase": pvc.status.phase if pvc.status else None,
            })
    return {"volumes": result}

# ---------- Ślad audytowy: odczyt dla administratora ----------
def serialize_audit_entry(row: "AuditLogDB") -> dict:
    # `detail` bywa ucięty do AUDIT_DETAIL_MAX_CHARS, więc nie musi być poprawnym JSON-em —
    # wtedy oddajemy surowy tekst zamiast wywalać całą listę na jednym wpisie.
    def parse_json_field(text):
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return {"raw": text}

    return {
        "id": row.id,
        "at": row.at.isoformat() + "Z" if row.at else None,
        "actor": row.actor,
        "actor_role": row.actor_role,
        "action": row.action,
        "target": row.target,
        "method": row.method,
        "path": row.path,
        "status_code": row.status_code,
        "outcome": row.outcome,
        "detail": parse_json_field(row.detail),
        "client_ip": row.client_ip,
        "user_agent": row.user_agent,
    }


@app.get("/audit-log", dependencies=[Depends(check_role(["admin"]))])
def read_audit_log(
    hours: int = Query(168, ge=1, le=24 * 366),
    actor: Optional[str] = None,
    action: Optional[str] = None,
    outcome: Optional[str] = None,
    target: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """Historia działań w systemie: kto, kiedy, co i z jakim skutkiem. Tylko administrator.

    Zwraca też listy występujących osób i akcji dla danego okna czasu, aby konsola mogła
    zbudować filtry bez drugiego żądania.
    """
    since = datetime.utcnow() - timedelta(hours=hours)
    db = SessionLocal()
    try:
        base = db.query(AuditLogDB).filter(AuditLogDB.at >= since)
        if actor:
            base = base.filter(AuditLogDB.actor == actor)
        if action:
            base = base.filter(AuditLogDB.action == action)
        if outcome:
            base = base.filter(AuditLogDB.outcome == outcome)
        if target:
            base = base.filter(AuditLogDB.target.ilike(f"%{target}%"))
        if search:
            like = f"%{search}%"
            base = base.filter(or_(
                AuditLogDB.actor.ilike(like),
                AuditLogDB.action.ilike(like),
                AuditLogDB.target.ilike(like),
                AuditLogDB.path.ilike(like),
                AuditLogDB.detail.ilike(like),
            ))
        total = base.count()
        rows = base.order_by(AuditLogDB.at.desc(), AuditLogDB.id.desc()).offset(offset).limit(limit).all()
        window = db.query(AuditLogDB).filter(AuditLogDB.at >= since)
        actors = sorted({value for (value,) in window.with_entities(AuditLogDB.actor).distinct() if value})
        actions = sorted({value for (value,) in window.with_entities(AuditLogDB.action).distinct() if value})
        return {
            "entries": [serialize_audit_entry(row) for row in rows],
            "total": total,
            "hours": hours,
            "limit": limit,
            "offset": offset,
            "actors": actors,
            "actions": actions,
            # Konsola musi umieć wytłumaczyć PUSTĄ listę: bez tego "brak wpisów" wygląda
            # identycznie, gdy audyt jest wyłączony i gdy naprawdę nikt nic nie zrobił.
            "enabled": AUDIT_ENABLED,
            "includes_reads": AUDIT_LOG_READS,
            "retention_seconds": AUDIT_TTL_SECONDS,
        }
    finally:
        db.close()


# ---------- Admin purge-data: usuwanie testowych danych (mlm-live-* prefix) ----------
@app.post("/admin/purge-data", dependencies=[Depends(check_role(["admin"]))])
def purge_data(req: PurgeDataRequest):
    """Purges all mlm-live-* prefixed items from all database tables.

    Feature-gated by MLM_ENABLE_TEST_PURGE and requires explicit 'purge-all-data' confirmation.
    Intended only for test data cleanup — removes tasks, jobs, queued_jobs, task_outcomes,
    images, reservations, and audit log entries matching the test prefix.
    """
    if not MLM_ENABLE_TEST_PURGE:
        raise HTTPException(status_code=403, detail="Test purge is disabled (MLM_ENABLE_TEST_PURGE not set)")
    if req.confirmation != "purge-all-data":
        raise HTTPException(status_code=400, detail="Confirmation must be exactly 'purge-all-data'")

    audit_note(action="admin_purge", confirmation_token="***")
    db = SessionLocal()
    try:
        # Tables that may contain mlm-live-* prefixed items
        purged = {}

        # tasks: match by name prefix
        count = db.query(TaskDB).filter(TaskDB.name.like("mlm-live-%")).delete(synchronize_session=False)
        if count:
            purged["tasks"] = count

        # task_outcomes: match by task_name prefix
        count = db.query(TaskOutcome).filter(TaskOutcome.task_name.like("mlm-live-%")).delete(synchronize_session=False)
        if count:
            purged["task_outcomes"] = count

        # jobs: match by task_name (nullable) prefix
        count = db.query(JobDB).filter(JobDB.task_name.like("mlm-live-%")).delete(synchronize_session=False)
        if count:
            purged["jobs"] = count

        # queued_jobs: match by display_name prefix
        count = db.query(QueuedJobDB).filter(QueuedJobDB.display_name.like("mlm-live-%")).delete(synchronize_session=False)
        if count:
            purged["queued_jobs"] = count

        # images: match by name or repository prefix
        count = db.query(ImageDB).filter(
            or_(ImageDB.name.like("mlm-live-%"), ImageDB.repository.like("mlm-live-%"))
        ).delete(synchronize_session=False)
        if count:
            purged["images"] = count

        # reservations: match by user prefix (crude heuristic for test data)
        count = db.query(Reservation).filter(Reservation.user_id.like("mlm-live-%")).delete(synchronize_session=False)
        if count:
            purged["reservations"] = count

        # audit_log: match by target or actor prefix
        count = db.query(AuditLogDB).filter(
            or_(AuditLogDB.target.like("mlm-live-%"), AuditLogDB.actor.like("mlm-live-%"))
        ).delete(synchronize_session=False)
        if count:
            purged["audit_log"] = count

        db.commit()
        total_deleted = sum(purged.values())
        return {
            "message": f"Purged {total_deleted} mlm-live-* test data items",
            "purged_by_table": purged,
            "total": total_deleted,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error("Purge failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Purge failed: {str(e)}")
    finally:
        db.close()


# ---------- User profile and notification settings ----------
@app.put("/users/{username}", dependencies=[Depends(check_role(["admin"]))])
def update_user(username: str, upd: UserUpdate):
    """Aktualizuje profil użytkownika: e-mail/Slack (powiadomienia), zespół (analiza/shared storage), priorytet."""
    # W audycie zapisujemy TYLKO pola faktycznie podane (z wartościami) — inaczej każdy zapis
    # formularza wyglądałby jak zmiana wszystkiego i nie dałoby się odczytać, co komu podniesiono.
    # Rozpisane bez API pydantica, bo to samo main.py działa pod pydantic 1 i 2.
    audit_note(changed={
        field: value for field, value in (
            ("email", upd.email), ("slack_id", upd.slack_id), ("team", upd.team),
            ("priority", upd.priority), ("gpu_quota_by_type", upd.gpu_quota_by_type),
            ("vram_quota_by_type", upd.vram_quota_by_type),
        ) if value is not None
    } or None)
    db = SessionLocal()
    try:
        user = db.query(UserDB).filter(UserDB.username == username).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if upd.email is not None:
            user.email = upd.email
        if upd.slack_id is not None:
            user.slack_id = upd.slack_id
        if upd.team is not None:
            user.team = upd.team
        if upd.priority is not None:
            user.priority = upd.priority
        if upd.gpu_quota_by_type is not None:
            user.gpu_quota_by_type = json.dumps(upd.gpu_quota_by_type)
        if upd.vram_quota_by_type is not None:
            user.vram_quota_by_type = json.dumps(upd.vram_quota_by_type)
        db.commit()
        db.refresh(user)
        return {"message": "User updated", "user": serialize_user(user)}
    except HTTPException:
        db.rollback()
        raise
    finally:
        db.close()

@app.delete("/users/{username}", dependencies=[Depends(check_role(["admin"]))])
def delete_user(username: str, current_user: UserDB = Depends(get_current_user)):
    """Usuwa konto użytkownika. Historyczne rekordy jobów/zadań (referencja po nazwie) pozostają dla audytu.

    Zabezpieczenia: nie można usunąć własnego konta ani ostatniego administratora (ochrona przed lockoutem).
    """
    if username == current_user.username:
        raise HTTPException(status_code=400, detail="You cannot delete your own account")
    db = SessionLocal()
    try:
        user = db.query(UserDB).filter(UserDB.username == username).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if user.role == "admin":
            admin_count = db.query(UserDB).filter(UserDB.role == "admin").count()
            if admin_count <= 1:
                raise HTTPException(status_code=400, detail="Cannot delete the last admin account")
        db.delete(user)
        db.commit()
        return {"message": "User deleted", "username": username}
    except HTTPException:
        db.rollback()
        raise
    finally:
        db.close()

# ---------- Grupy i projekty z limitami GPU (egzekwowane przez enforce_quota) ----------
def sync_group_members(db: Session, group_name: str, members: Optional[List[str]]):
    """Ustawia członkostwo grupy przez pole users.team (spójne z limitami/analizą/shared storage).

    Przypisuje wskazanych użytkowników do grupy i usuwa z niej tych, których już nie ma na liście.
    Gdy members=None, nie zmienia członkostwa (aktualizowane są tylko limity grupy).
    """
    if members is None:
        return
    desired = {m for m in members if m}
    if desired:
        for u in db.query(UserDB).filter(UserDB.username.in_(desired)).all():
            u.team = group_name
    for u in db.query(UserDB).filter(UserDB.team == group_name).all():
        if u.username not in desired:
            u.team = None

def group_member_names(db: Session, group_name: str) -> List[str]:
    return [u.username for u in db.query(UserDB).filter(UserDB.team == group_name).order_by(UserDB.username).all()]

@app.post("/groups", dependencies=[Depends(check_role(["admin"]))])
def create_group(g: GroupCreate):
    audit_note(target=g.name, total_gpus=g.total_gpus, total_disk_gb=g.total_disk_gb,
               gpu_quota_by_type=g.gpu_quota_by_type, members=g.members)
    db = SessionLocal()
    try:
        existing = db.query(GroupDB).filter(GroupDB.name == g.name).first()
        rec = existing or GroupDB(name=g.name)
        rec.total_gpus = g.total_gpus; rec.total_disk_gb = g.total_disk_gb
        rec.gpu_quota_by_type = json.dumps(g.gpu_quota_by_type) if g.gpu_quota_by_type else None
        if not existing:
            db.add(rec)
        sync_group_members(db, g.name, g.members)
        db.commit()
        return {"message": "Group saved", "group": {"name": g.name, "total_gpus": g.total_gpus,
                "total_disk_gb": g.total_disk_gb, "gpu_quota_by_type": g.gpu_quota_by_type,
                "members": group_member_names(db, g.name)}}
    finally:
        db.close()

@app.get("/groups", dependencies=[Depends(check_role(["user", "poweruser", "admin", "readonly"]))])
def list_groups():
    db = SessionLocal()
    rows = [{"name": x.name, "total_gpus": x.total_gpus, "total_disk_gb": x.total_disk_gb,
             "gpu_quota_by_type": json.loads(x.gpu_quota_by_type) if x.gpu_quota_by_type else None,
             "members": group_member_names(db, x.name)}
            for x in db.query(GroupDB).order_by(GroupDB.name).all()]
    db.close()
    return {"groups": rows}

@app.put("/groups/{name}", dependencies=[Depends(check_role(["admin"]))])
def update_group(name: str, g: GroupCreate):
    """Aktualizuje limity grupy i (opcjonalnie) jej członkostwo. Nazwa z URL jest wiążąca."""
    audit_note(total_gpus=g.total_gpus, total_disk_gb=g.total_disk_gb,
               gpu_quota_by_type=g.gpu_quota_by_type, members=g.members)
    db = SessionLocal()
    try:
        rec = db.query(GroupDB).filter(GroupDB.name == name).first()
        if not rec:
            raise HTTPException(status_code=404, detail="Group not found")
        rec.total_gpus = g.total_gpus
        rec.total_disk_gb = g.total_disk_gb
        rec.gpu_quota_by_type = json.dumps(g.gpu_quota_by_type) if g.gpu_quota_by_type else None
        sync_group_members(db, name, g.members)
        db.commit()
        return {"message": "Group updated", "group": {"name": name, "total_gpus": rec.total_gpus,
                "total_disk_gb": rec.total_disk_gb,
                "gpu_quota_by_type": g.gpu_quota_by_type,
                "members": group_member_names(db, name)}}
    except HTTPException:
        db.rollback()
        raise
    finally:
        db.close()

@app.delete("/groups/{name}", dependencies=[Depends(check_role(["admin"]))])
def delete_group(name: str):
    """Usuwa grupę. Członkostwa są zdejmowane (users.team=None); użytkownicy, joby i storage pozostają."""
    db = SessionLocal()
    try:
        rec = db.query(GroupDB).filter(GroupDB.name == name).first()
        if not rec:
            raise HTTPException(status_code=404, detail="Group not found")
        for u in db.query(UserDB).filter(UserDB.team == name).all():
            u.team = None
        db.delete(rec)
        db.commit()
        return {"message": "Group deleted", "group": name}
    except HTTPException:
        db.rollback()
        raise
    finally:
        db.close()

def project_members(rec: "ProjectDB") -> list:
    """Lista członków projektu (z kolumny JSON). Pusta, gdy nie ustawiono."""
    if not rec or not rec.members:
        return []
    try:
        parsed = json.loads(rec.members)
        return [str(m) for m in parsed] if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def normalize_members(members: Optional[List[str]]) -> Optional[str]:
    """Serializuje listę członków do JSON (deduplikacja, bez pustych wpisów)."""
    if members is None:
        return None
    seen, out = set(), []
    for m in members:
        m = (m or "").strip()
        if m and m not in seen:
            seen.add(m); out.append(m)
    return json.dumps(out)


def ensure_project_access(project: Optional[str], current_user: UserDB):
    """403, jeśli użytkownik nie ma dostępu do projektu (nie jest właścicielem ani członkiem).

    Bez tego dowolny użytkownik mógł zgłaszać zadania na cudzy projekt (i tym samym zużywać
    jego limit GPU). Admin jest zwolniony. Projekt bez zdefiniowanych członków pozostaje
    otwarty dla wszystkich — zachowuje zgodność ze starszymi projektami bez członkostwa.
    """
    if not project or current_user.role == "admin":
        return
    db = SessionLocal()
    try:
        rec = db.query(ProjectDB).filter(ProjectDB.name == project).first()
        if not rec:
            return                      # nieznany projekt: walidacja limitów zajmie się resztą
        members = project_members(rec)
        if not members:
            return                      # brak listy członków => projekt otwarty (kompatybilność)
        if current_user.username == (rec.owner or "") or current_user.username in members:
            return
        raise HTTPException(status_code=403,
                            detail=f"You are not a member of project '{project}'")
    finally:
        db.close()


@app.post("/projects", dependencies=[Depends(check_role(["admin", "poweruser"]))])
def create_project(p: ProjectCreate):
    audit_note(target=p.name, owner=p.owner, total_gpus=p.total_gpus,
               shared_storage_gb=p.shared_storage_gb, gpu_quota_by_type=p.gpu_quota_by_type, members=p.members)
    db = SessionLocal()
    try:
        existing = db.query(ProjectDB).filter(ProjectDB.name == p.name).first()
        rec = existing or ProjectDB(name=p.name)
        rec.owner = p.owner; rec.total_gpus = p.total_gpus; rec.shared_storage_gb = p.shared_storage_gb
        rec.gpu_quota_by_type = json.dumps(p.gpu_quota_by_type) if p.gpu_quota_by_type else None
        if p.members is not None:
            rec.members = normalize_members(p.members)
        if not existing:
            db.add(rec)
        db.commit()
        db.refresh(rec)
        return {"message": "Project saved", "project": {"name": p.name, "owner": p.owner,
                "total_gpus": p.total_gpus, "shared_storage_gb": p.shared_storage_gb,
                "gpu_quota_by_type": p.gpu_quota_by_type,
                "members": project_members(rec)}}
    finally:
        db.close()

@app.get("/projects", dependencies=[Depends(check_role(["user", "poweruser", "admin", "readonly"]))])
def list_projects():
    db = SessionLocal()
    rows = [{"name": x.name, "owner": x.owner, "total_gpus": x.total_gpus, "shared_storage_gb": x.shared_storage_gb,
             "gpu_quota_by_type": json.loads(x.gpu_quota_by_type) if x.gpu_quota_by_type else None,
             "members": project_members(x)}
            for x in db.query(ProjectDB).order_by(ProjectDB.name).all()]
    db.close()
    return {"projects": rows}

@app.put("/projects/{name}", dependencies=[Depends(check_role(["admin", "poweruser"]))])
def update_project(name: str, p: ProjectCreate):
    """Aktualizuje projekt (właściciel, limity, członkowie). Nazwa z URL jest wiążąca.

    `members` jest teraz UTRWALANE w kolumnie projects.members (JSON) i egzekwowane przez
    ensure_project_access() przy zgłaszaniu zadań oraz w /analytics/usage. Pominięcie pola
    (None) zostawia dotychczasową listę bez zmian; pusta lista otwiera projekt dla wszystkich.
    """
    audit_note(owner=p.owner, total_gpus=p.total_gpus, shared_storage_gb=p.shared_storage_gb,
               gpu_quota_by_type=p.gpu_quota_by_type, members=p.members)
    db = SessionLocal()
    try:
        rec = db.query(ProjectDB).filter(ProjectDB.name == name).first()
        if not rec:
            raise HTTPException(status_code=404, detail="Project not found")
        rec.owner = p.owner
        rec.total_gpus = p.total_gpus
        rec.shared_storage_gb = p.shared_storage_gb
        rec.gpu_quota_by_type = json.dumps(p.gpu_quota_by_type) if p.gpu_quota_by_type else None
        if p.members is not None:
            rec.members = normalize_members(p.members)
        db.commit()
        db.refresh(rec)
        return {"message": "Project updated", "project": {"name": name, "owner": rec.owner,
                "total_gpus": rec.total_gpus, "shared_storage_gb": rec.shared_storage_gb,
                "gpu_quota_by_type": p.gpu_quota_by_type,
                "members": project_members(rec)}}
    except HTTPException:
        db.rollback()
        raise
    finally:
        db.close()

@app.delete("/projects/{name}", dependencies=[Depends(check_role(["admin", "poweruser"]))])
def delete_project(name: str):
    """Usuwa projekt. Historyczne joby/zadania zachowują nazwę projektu (audyt); storage pozostaje."""
    db = SessionLocal()
    try:
        rec = db.query(ProjectDB).filter(ProjectDB.name == name).first()
        if not rec:
            raise HTTPException(status_code=404, detail="Project not found")
        db.delete(rec)
        db.commit()
        return {"message": "Project deleted", "project": name}
    except HTTPException:
        db.rollback()
        raise
    finally:
        db.close()

# ---------- Access-controlled team shared storage ----------
@app.post("/teams/{team}/shared-storage", dependencies=[Depends(check_role(["admin", "poweruser"]))])
def create_team_shared_storage(team: str, size_gb: int = 50, current_user: UserDB = Depends(get_current_user)):
    """Create a ReadWriteMany PVC for a team in a dedicated namespace.

    Only administrators and power users may create it; team membership controls intended access.
    """
    team = sanitize_image_name(team)  # ten sam bezpieczny wzorzec nazw
    ns = f"team-{team}"
    if LOCAL_DEV_MODE:
        return {"message": "Team shared storage ready", "namespace": ns, "pvc": f"{team}-shared", "size_gb": size_gb, "local_dev": True}
    load_kubernetes_config()
    v1 = kubernetes.client.CoreV1Api()
    try:
        v1.create_namespace(body={"metadata": {"name": ns, "labels": {"devops.dev/team": team}}})
    except kubernetes.client.exceptions.ApiException as e:
        if e.status != 409:
            raise HTTPException(status_code=500, detail=str(e))
    pvc = {
        "metadata": {"name": f"{team}-shared", "labels": {"devops.dev/team": team, "devops.dev/volume-type": "shared"}},
        "spec": {"accessModes": ["ReadWriteMany"], **pvc_storage_class_spec(),
                 "resources": {"requests": {"storage": f"{size_gb}Gi"}}},
    }
    try:
        v1.create_namespaced_persistent_volume_claim(namespace=ns, body=pvc)
    except kubernetes.client.exceptions.ApiException as e:
        if e.status != 409:
            raise HTTPException(status_code=500, detail=str(e))
    return {"message": "Team shared storage ready", "namespace": ns, "pvc": f"{team}-shared", "size_gb": size_gb}

@app.get("/alerts", dependencies=[Depends(check_role(["user", "poweruser", "admin", "readonly"]))])
def get_alerts(dismissed: bool = False, current_user: UserDB = Depends(get_current_user)):
    """Pobiera aktywne alerty GPU dotyczące przekroczenia progów (temperatura, OOM, moc).

    Querystrings:
    - dismissed: Jeśli True, zwraca potwierdzone alerty; jeśli False, zwraca aktywne

    Zwraca listę alertów w formacie oczekiwanym przez frontend.
    """
    db = SessionLocal()
    try:
        if dismissed:
            # Zwraca potwierdzone alerty z ostatnich 24 godzin
            since = datetime.utcnow() - timedelta(hours=24)
            dismissed_records = db.query(DismissedAlertDB).filter(
                DismissedAlertDB.dismissed_at >= since
            ).all()
            return {
                "alerts": [
                    {
                        "id": a.id,
                        "alert_name": a.alert_name,
                        "gpu_uuid": a.gpu_uuid,
                        "dismissed_at": a.dismissed_at.isoformat(),
                    }
                    for a in dismissed_records
                ]
            }

        # Pobiera aktywne alerty z Prometheusa
        try:
            resp = requests.get(
                f"{PROMETHEUS_URL}/api/v1/query",
                params={"query": 'ALERTS{alertstate="firing"}'},
                timeout=5
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.debug(f"Prometheus query failed: {e}")
            return {"alerts": []}

        if data.get("status") != "success":
            logger.debug(f"Prometheus query status not success: {data}")
            return {"alerts": []}

        # Mapa nazw alertów na poziomy ważności
        severity_map = {
            "GPUHighTemperature": "critical",
            "GPUOOM": "critical",
            "GPUHighPower": "warning",
        }

        # Mapa nazw alertów na jednostki
        unit_map = {
            "GPUHighTemperature": "°C",
            "GPUOOM": "MB",
            "GPUHighPower": "W",
        }

        # Pobiera listę potwierdzonych alertów z bazy
        since = datetime.utcnow() - timedelta(hours=24)
        dismissed_records = db.query(DismissedAlertDB).filter(
            DismissedAlertDB.dismissed_at >= since
        ).all()
        dismissed_ids = {a.id for a in dismissed_records}

        # Konwertuje alerty z Prometheusa na format frontendu
        alerts = []
        for result in data.get("data", {}).get("result", []):
            metric = result.get("metric", {})
            alert_name = metric.get("alertname", "unknown")

            if alert_name not in severity_map:
                continue

            gpu_uuid = metric.get("gpu") or metric.get("UUID") or "unknown"
            node = metric.get("node") or metric.get("Hostname")
            value = float(result["value"][1]) if result.get("value") else None

            # Pobiera próg z adnotacji lub ustawia domyślne
            thresholds = {
                "GPUHighTemperature": 85,
                "GPUOOM": 100,
                "GPUHighPower": 250,
            }

            alert_id = f"{alert_name}-{gpu_uuid}"

            # Pomija alerty, które zostały potwierdzone w ostatnich 24 godzinach
            if alert_id in dismissed_ids:
                continue

            alert_obj = {
                "id": alert_id,
                "alert_name": alert_name,
                "severity": severity_map.get(alert_name, "info"),
                "gpu_uuid": gpu_uuid,
                "node": node,
                "value": value,
                "threshold": thresholds.get(alert_name),
                "unit": unit_map.get(alert_name),
                "message": metric.get("summary", f"{alert_name} na GPU {gpu_uuid}"),
                "fired_at": datetime.utcnow().isoformat(),
            }
            alerts.append(alert_obj)

        return {"alerts": alerts}

    except Exception as e:
        logger.error(f"Failed to get alerts: {e}")
        return {"alerts": []}
    finally:
        db.close()


@app.post("/alerts/{alert_id}/dismiss", dependencies=[Depends(check_role(["user", "poweruser", "admin"]))])
def dismiss_alert(alert_id: str, current_user: UserDB = Depends(get_current_user)):
    """Potwierdza alert — użytkownik widział go i go zatwierdza.

    Alert nie będzie wyświetlany przez następne 24 godziny, chyba że zniknie z Prometheusa
    i pojawi się ponownie.

    Parametry:
    - alert_id: ID alertu do potwierdzenia (np. "GPUHighTemperature-gpu-uuid")

    Zwraca:
    {
        "message": "Alert dismissed"
    }
    """
    db = SessionLocal()
    try:
        # Sprawdza, czy alert już został potwierdzony
        existing = db.query(DismissedAlertDB).filter(
            DismissedAlertDB.id == alert_id
        ).first()

        if not existing:
            # Parsuje alert_id na alert_name i gpu_uuid
            parts = alert_id.split("-", 1)
            alert_name = parts[0] if len(parts) > 0 else alert_id
            gpu_uuid = "-".join(parts[1:]) if len(parts) > 1 else None

            # Tworzy nowy wpis w bazie
            dismissed = DismissedAlertDB(
                id=alert_id,
                alert_name=alert_name,
                gpu_uuid=gpu_uuid,
                dismissed_by=current_user.username,
            )
            db.add(dismissed)
            db.commit()
            audit_note(action="alerts.dismiss", target=alert_id)

        return {"message": "Alert dismissed"}

    except Exception as e:
        logger.error(f"Failed to dismiss alert: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to dismiss alert")
    finally:
        db.close()


@app.get("/alerts/stats", dependencies=[Depends(check_role(["admin"]))])
def get_alert_stats():
    """Zwraca statystyki aktualnych alertów GPU (tylko dla admin).

    Zwraca:
    {
        "total_active": 5,
        "critical": 2,
        "warning": 3,
        "by_alert_type": {
            "GPUHighTemperature": 2,
            "GPUOOM": 3
        },
        "affected_gpus": ["gpu-1", "gpu-2"]
    }
    """
    try:
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": 'ALERTS{alertstate="firing"}'},
            timeout=5
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"Failed to query Prometheus for stats: {e}")
        return {
            "total_active": 0,
            "critical": 0,
            "warning": 0,
            "by_alert_type": {},
            "affected_gpus": []
        }

    if data.get("status") != "success":
        return {
            "total_active": 0,
            "critical": 0,
            "warning": 0,
            "by_alert_type": {},
            "affected_gpus": []
        }

    severity_map = {
        "GPUHighTemperature": "critical",
        "GPUOOM": "critical",
        "GPUHighPower": "warning",
    }

    alerts = []
    for result in data.get("data", {}).get("result", []):
        metric = result.get("metric", {})
        alert_name = metric.get("alertname", "unknown")

        if alert_name not in severity_map:
            continue

        gpu_uuid = metric.get("gpu") or metric.get("UUID") or "unknown"
        alerts.append({
            "alert_name": alert_name,
            "gpu_uuid": gpu_uuid,
            "severity": severity_map.get(alert_name, "info")
        })

    stats = {
        "total_active": len(alerts),
        "critical": len([a for a in alerts if a["severity"] == "critical"]),
        "warning": len([a for a in alerts if a["severity"] == "warning"]),
        "by_alert_type": {},
        "affected_gpus": list(set(a.get("gpu_uuid") for a in alerts if a.get("gpu_uuid")))
    }

    for alert in alerts:
        alert_name = alert["alert_name"]
        stats["by_alert_type"][alert_name] = stats["by_alert_type"].get(alert_name, 0) + 1

    return stats


@app.get("/metrics")
def metrics():
    return prometheus_client.generate_latest()
