#!/bin/bash
set -euo pipefail

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}=== Deploying DevOps Backend ===${NC}"
# Środowisko: dummy (minikube) vs professional (istniejący klaster). MLM_ENV=professional pomija minikube.
MLM_ENV="${MLM_ENV:-dummy}"
BUILD_IMAGES="${BUILD_IMAGES:-1}"
MLM_CONFIG_ONLY="${MLM_CONFIG_ONLY:-0}"
IMAGE_REGISTRY="${IMAGE_REGISTRY:-}"
IMAGE_TAG="${IMAGE_TAG:-local}"
PUSH_IMAGE=0

if [ "$MLM_ENV" = "dummy" ]; then
    API_IMAGE="${API_IMAGE:-devops-api:local}"
    CONTROLLER_IMAGE="${CONTROLLER_IMAGE:-devops-controller:local}"
    IMAGE_PULL_POLICY="${IMAGE_PULL_POLICY:-IfNotPresent}"
elif [ "$BUILD_IMAGES" = "1" ]; then
    # OOBE: gdy nie podano IMAGE_REGISTRY, używamy wbudowanego rejestru klastra
    # (registry:2, NodePort localhost:32000), który wdraża other-setup.sh — dokładnie
    # tak jak addon `registry` w minikube dla trybu dummy. Dzięki temu tryb professional
    # jest w pełni automatyczny (zero konfiguracji przez użytkownika). Aby użyć innego
    # rejestru (np. firmowego Harbora / Docker Huba), ustaw IMAGE_REGISTRY=... .
    if [ -z "$IMAGE_REGISTRY" ]; then
        IMAGE_REGISTRY="${DEFAULT_IMAGE_REGISTRY:-localhost:32000}"
        echo "env=professional: IMAGE_REGISTRY nie podano — używam wbudowanego rejestru klastra ($IMAGE_REGISTRY)."
    fi
    registry="${IMAGE_REGISTRY%/}"
    API_IMAGE="${API_IMAGE:-$registry/devops-api:$IMAGE_TAG}"
    CONTROLLER_IMAGE="${CONTROLLER_IMAGE:-$registry/devops-controller:$IMAGE_TAG}"
    IMAGE_PULL_POLICY="${IMAGE_PULL_POLICY:-Always}"
    PUSH_IMAGE=1
else
    : "${API_IMAGE:?Set API_IMAGE when MLM_ENV=professional and BUILD_IMAGES=0}"
    : "${CONTROLLER_IMAGE:?Set CONTROLLER_IMAGE when MLM_ENV=professional and BUILD_IMAGES=0}"
    IMAGE_PULL_POLICY="${IMAGE_PULL_POLICY:-IfNotPresent}"
fi

case "$API_IMAGE$CONTROLLER_IMAGE$IMAGE_PULL_POLICY" in
    *'|'*|*'&'*|*$'\n'*|*' '*) echo "Image settings contain unsupported whitespace or separators" >&2; exit 2 ;;
esac
case "$IMAGE_PULL_POLICY" in
    Always|IfNotPresent|Never) ;;
    *) echo "IMAGE_PULL_POLICY must be Always, IfNotPresent, or Never" >&2; exit 2 ;;
esac

# Wykrywanie GPU — pomija kroki GPU na maszynach bez karty (HAS_GPU=1/0 wymusza).
HAS_GPU="${HAS_GPU:-}"
if [ -z "$HAS_GPU" ]; then
    if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then HAS_GPU=1; else HAS_GPU=0; fi
fi
[ "$HAS_GPU" = "1" ] && echo "GPU: wykryto" || echo "GPU: brak — pomijam kroki GPU (CPU-only)"
# Tryb symulacji GPU: HAS_GPU=1 ale brak realnego sterownika (nvidia-smi) => nie da
# się zainstalować GPU Operatora (wymaga sterowników). Wtedy symulujemy GPU
# etykietami węzła (simulate-gpu.sh) i pomijamy helm GPU Operator. Wymuszenie: GPU_SIM=1/0.
GPU_SIM="${GPU_SIM:-}"
if [ -z "$GPU_SIM" ]; then
    if [ "$HAS_GPU" = "1" ] && ! { command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; }; then
        GPU_SIM=1
    else
        GPU_SIM=0
    fi
fi
[ "$GPU_SIM" = "1" ] && echo "GPU: tryb SYMULACJI (brak realnego sterownika) — pomijam helm GPU Operator, symuluję węzeł."
MK_GPU=""; [ "$HAS_GPU" = "1" ] && [ "$GPU_SIM" != "1" ] && MK_GPU="--gpus=all"

if [ "$MLM_CONFIG_ONLY" = "1" ]; then
    printf 'MLM_ENV=%s\nHAS_GPU=%s\nGPU_SIM=%s\nAPI_IMAGE=%s\nCONTROLLER_IMAGE=%s\nIMAGE_PULL_POLICY=%s\nBUILD_IMAGES=%s\n' \
      "$MLM_ENV" "$HAS_GPU" "$GPU_SIM" "$API_IMAGE" "$CONTROLLER_IMAGE" "$IMAGE_PULL_POLICY" "$BUILD_IMAGES"
    exit 0
fi

if [ "$MLM_ENV" = "dummy" ]; then
    echo "STARTING MINIKUBE (env=dummy)"
    if [[ -n $(minikube status | grep Stopped) ]]; then
            echo "Minikube was turned off, turning it again on."
        minikube start $MK_GPU
    else
        echo "Minikube is turned on, skipping starting procedure."
    fi
    if [[ -n $(minikube status | grep Stopped) ]]; then
        echo "Minikube turning on"
        sleep 5
    fi
else
    echo "env=professional: using existing Kubernetes cluster (skipping minikube)."
    kubectl cluster-info >/dev/null 2>&1 || { echo -e "${RED}No reachable cluster (kubectl cluster-info).${NC}"; exit 1; }
fi

# Sprawdzenie Helm
if ! command -v helm &> /dev/null; then
    echo -e "${RED}Helm not found. Please install Helm.${NC}"
    exit 1
fi

# Namespaces
kubectl create namespace gpu-operator --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace devops-system --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -
# Real GPU deployments install enforcement components. Simulation only publishes
# inventory labels; it never claims a schedulable extended resource.
if [ "$HAS_GPU" = "1" ] && [ "$GPU_SIM" != "1" ]; then
    echo "Creating VRAM webhook and GPU Operator resources..."
    kubectl create configmap vram-webhook-script --namespace devops-system \
      --from-file=webhook.py=../gpu-operator/webhook.py \
      --dry-run=client -o yaml | kubectl apply -f -
    VRAM_WEBHOOK_FAILURE_POLICY="${VRAM_WEBHOOK_FAILURE_POLICY:-Fail}" \
      bash ../scripts/generate-webhook-certs.sh
    kubectl apply -f ../gpu-operator/vram-webhook.yaml
    kubectl rollout restart deployment/vram-webhook -n devops-system
    # MUSIMY poczekać, aż webhook znów odpowiada, PRZED tworzeniem czegokolwiek dalej.
    # Przy failurePolicy=Fail niedostępny webhook powoduje odrzucanie tworzenia podów w
    # namespace'ach objętych jego zakresem — deploy szedł dalej w tym właśnie okienku.
    kubectl rollout status deployment/vram-webhook -n devops-system --timeout=120s \
      || echo -e "${RED}WARN: vram-webhook nie wstał — tworzenie podów zadań będzie odrzucane (failurePolicy=Fail).${NC}"

    kubectl apply -f ../gpu-operator/custom-mig-parted-config.yaml
    # Profile device-plugina. Klucz `default` (bez sekcji sharing) to stan wyjściowy:
    # każda karta jest jednym całym urządzeniem. Podział dokłada API per karta.
    # Ponowny deploy NIE zdejmuje podziałów ustawionych przez API: `kubectl apply` zostawia
    # dopisane przez nie klucze `mlm-<node>`, a węzły dalej wskazują swój profil etykietą.
    kubectl apply -f ../gpu-operator/time-slicing-config.yaml
    echo "Installing Nvidia GPU Operator..."
    # Zestaw metryk dcgm-exportera BEZ pól profilujących (DCP). Domyślny plik obrazu
    # (dcp-metrics-included.csv) na części kart/sterowników wywala eksporter w pętli
    # ("The third-party Profiling module returned an unrecoverable error"), co zabiera
    # WSZYSTKIE metryki GPU — a więc puste /analytics/usage, /gpu/usage i alerty.
    kubectl apply -f ../monitoring/dcgm-metrics.yaml
    helm repo add nvidia https://helm.ngc.nvidia.com/nvidia --force-update
    helm upgrade --install gpu-operator nvidia/gpu-operator \
      --namespace gpu-operator \
      --set devicePlugin.config.name=time-slicing-config \
      --set devicePlugin.config.default=default \
      --set migManager.enabled=true \
      --set migManager.config.name=custom-mig-parted-config \
      --set mig.strategy=mixed \
      --set dcgm.enabled=true \
      --set dcgmExporter.enabled=true \
      --set dcgmExporter.config.name=dcgm-metrics
elif [ "$GPU_SIM" = "1" ]; then
    echo "GPU simulation: publishing inventory-only labels; no webhook, operator, or fake capacity."
    MLM_ENV="$MLM_ENV" bash ../scripts/simulate-gpu.sh
else
    echo "CPU-only deploy: skipping VRAM webhook, GPU Operator, MIG, and time-slicing."
fi

# CRD
echo "Applying CRDs..."
for crd in ../crd/*.yaml; do
    kubectl apply -f "$crd"
done

# Kontroler Kopf
echo "Deploying Kopf controller..."
kubectl apply -f ../controllers/controller-rbac.yaml

if [ "$BUILD_IMAGES" = "1" ]; then
    command -v docker >/dev/null 2>&1 || { echo "docker is required when BUILD_IMAGES=1" >&2; exit 1; }
    MLM_ENV="$MLM_ENV" CONTROLLER_IMAGE="$CONTROLLER_IMAGE" PUSH_IMAGE="$PUSH_IMAGE" \
      bash ../scripts/build-controller-image.sh
fi
kubectl create configmap controller-scripts --namespace devops-system \
  --from-file=task_controller.py=../controllers/task_controller.py \
  --dry-run=client -o yaml | kubectl apply -f -
sed -e "s|image: devops-controller:local|image: $CONTROLLER_IMAGE|" \
    -e "s|imagePullPolicy: IfNotPresent|imagePullPolicy: $IMAGE_PULL_POLICY|" \
    ../controllers/controller-deployment.yaml | kubectl apply -f -

# PostgreSQL i API
echo "Deploying PostgreSQL and API..."
kubectl apply -f ../api/postgres-deployment.yaml
if [ "$BUILD_IMAGES" = "1" ]; then
    command -v docker >/dev/null 2>&1 || { echo "docker is required when BUILD_IMAGES=1" >&2; exit 1; }
    MLM_ENV="$MLM_ENV" API_IMAGE="$API_IMAGE" PUSH_IMAGE="$PUSH_IMAGE" \
      bash ../scripts/build-api-image.sh
fi
kubectl create configmap api-script --namespace devops-system --from-file=main.py=../api/main.py --dry-run=client -o yaml | kubectl apply -f -
sed -e "s|image: devops-api:local|image: $API_IMAGE|" \
    -e "s|imagePullPolicy: IfNotPresent|imagePullPolicy: $IMAGE_PULL_POLICY|" \
    ../api/api-deployment.yaml | kubectl apply -f -

# W środowisku professional rejestr jest pod innym adresem niż addon minikube —
# ustawiamy zmienne rejestru na API, by upload/pull obrazów działał bez ręcznej edycji.
if [ "$MLM_ENV" = "professional" ]; then
    echo "env=professional: konfiguruję adresy rejestru na deploymencie API..."
    # Rejestr, do którego węzły potrafią sięgnąć po HTTP (mirror w registries.yaml).
    # Używamy tej samej, obciętej z końcowego "/" nazwy co dla obrazów control-plane;
    # gdy BUILD_IMAGES=0, $IMAGE_REGISTRY jest PUSTE, więc mamy jawny fallback —
    # bez tego HELPER_IMG wychodziło jako "/busybox:latest" i docker tag się wywalał.
    HELPER_REGISTRY="${IMAGE_REGISTRY:-localhost:32000}"
    HELPER_REGISTRY="${HELPER_REGISTRY%/}"
    PULL_PREFIX="${REGISTRY_PULL_PREFIX:-$HELPER_REGISTRY}"
    kubectl set env deployment/devops-api -n devops-system \
      REGISTRY_PUSH_HOST="${REGISTRY_PUSH_HOST:-registry.registry.svc.cluster.local:80}" \
      REGISTRY_PULL_PREFIX="$PULL_PREFIX"

    # Klasa storage dla przestrzeni roboczych (<user>-home/-scratch/-project). API domyślnie
    # NIE ustawia nazwy klasy (używa domyślnej klasy klastra), co jest właściwe dla k3s
    # (local-path). Jeśli klaster nie ma klasy domyślnej, podaj WORKSPACE_STORAGE_CLASS.
    if [ -n "${WORKSPACE_STORAGE_CLASS:-}" ]; then
        echo "env=professional: WORKSPACE_STORAGE_CLASS=$WORKSPACE_STORAGE_CLASS"
        kubectl set env deployment/devops-api -n devops-system \
          WORKSPACE_STORAGE_CLASS="$WORKSPACE_STORAGE_CLASS"
    else
        # Usuwamy ewentualną starą wartość z poprzedniego wdrożenia (np. odziedziczone
        # "standard", które na k3s nie istnieje i zostawia PVC na zawsze w Pending).
        kubectl set env deployment/devops-api -n devops-system WORKSPACE_STORAGE_CLASS- >/dev/null 2>&1 || true
        if ! kubectl get sc -o jsonpath='{.items[*].metadata.annotations}' 2>/dev/null \
             | grep -q 'is-default-class":"true"'; then
            echo -e "${RED}WARN: klaster nie ma domyślnej StorageClass — PVC (baza, przestrzenie robocze, wyniki) zostaną w Pending. Ustaw WORKSPACE_STORAGE_CLASS=<klasa>.${NC}"
            kubectl get sc || true
        fi
    fi

    # Pod results-helper (odczyt/pakowanie wyników z PVC użytkownika) domyślnie używa
    # busybox:latest z Docker Huba. Na klastrze professional węzły często NIE mogą go
    # pobrać (docker.io zablokowany/limitowany) -> helper utyka, a odczyt wyników wisi.
    # Rozwiązanie OOBE: mirroring busybox do wewnętrznego rejestru (localhost:32000, któremu
    # containerd ufa) i wskazujemy go API. Węzły pobierają go wtedy lokalnie, bez Docker Huba.
    if command -v docker >/dev/null 2>&1; then
        HELPER_IMG="${RESULTS_READER_IMAGE:-$HELPER_REGISTRY/busybox:latest}"
        BASE_BUSYBOX="${RESULTS_HELPER_BASE_IMAGE:-busybox:latest}"
        echo "Mirroring $BASE_BUSYBOX -> $HELPER_IMG (rejestr klastra) dla results-helpera..."
        if docker pull "$BASE_BUSYBOX" && docker tag "$BASE_BUSYBOX" "$HELPER_IMG" && docker push "$HELPER_IMG"; then
            kubectl set env deployment/devops-api -n devops-system RESULTS_READER_IMAGE="$HELPER_IMG"
        else
            echo -e "${RED}WARN: nie udało się zlustrzać busybox do $HELPER_REGISTRY — odczyt wyników może nie działać, jeśli węzły nie mają dostępu do Docker Huba.${NC}"
        fi
    fi
fi

# Source files are mounted from ConfigMaps, so restart both Deployments to load updated code.
echo "Restartuję devops-api i devops-controller, by wczytały aktualny kod z ConfigMap..."
kubectl rollout restart deployment/devops-api deployment/devops-controller -n devops-system
kubectl rollout status deployment/devops-api -n devops-system --timeout=180s || true
kubectl rollout status deployment/devops-controller -n devops-system --timeout=180s || true

# Remove on-demand results-helper Pods so the next request recreates them with current settings.
echo "Reset: usuwam pody results-helper (odtworzą się na żądanie z aktualnym obrazem)..."
kubectl delete pod -l app=results-helper --all-namespaces --ignore-not-found >/dev/null 2>&1 || true

# Remove terminal Pods and unused image layers to limit disk growth. Disable with SKIP_DISK_CLEANUP=1.
if [ "${SKIP_DISK_CLEANUP:-0}" != "1" ]; then
    echo "Higiena dysku: usuwam zakończone/eksmitowane pody i nieużywane obrazy..."
    # Zakończone i nieudane pody zadań (rekordy w bazie pozostają).
    kubectl delete pod --all-namespaces --field-selector=status.phase=Succeeded --ignore-not-found >/dev/null 2>&1 || true
    kubectl delete pod --all-namespaces --field-selector=status.phase=Failed --ignore-not-found >/dev/null 2>&1 || true
    # Prune obrazów w TYM demonie, do którego budowaliśmy: minikube (dummy) lub host docker (professional).
    if command -v docker >/dev/null 2>&1; then
        if [ "$MLM_ENV" = "dummy" ] && command -v minikube >/dev/null 2>&1; then
            ( eval "$(minikube -p minikube docker-env 2>/dev/null)" && docker image prune -f >/dev/null 2>&1 ) || true
        else
            docker image prune -f >/dev/null 2>&1 || true
        fi
    fi
    # W klastrze (k3s/containerd) usuń nieużywane obrazy z węzła, jeśli crictl jest dostępny.
    if command -v crictl >/dev/null 2>&1; then
        sudo crictl rmi --prune >/dev/null 2>&1 || crictl rmi --prune >/dev/null 2>&1 || true
    fi
fi

# Prometheus stack
echo "Installing Prometheus stack..."
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update
# UWAGA: bez dodatkowych --set dla Grafany. Wszystko jest w prometheus-values.yaml.
# `--set grafana.additionalDataSources={}` kasowało źródło danych (Grafana bez datasource,
# puste panele), a `--set grafana.defaultDashboardsEnabled=false` tylko powtarzało values.
helm upgrade --install monitoring prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  -f ../monitoring/prometheus-values.yaml
# Dashboard Grafana (jeśli istnieje)
if [ -f ../monitoring/gpu-dashboard.json ]; then
    kubectl create configmap gpu-dashboard -n monitoring \
      --from-file=../monitoring/gpu-dashboard.json \
      --dry-run=client -o yaml | kubectl apply -f -
    kubectl label configmap gpu-dashboard grafana_dashboard=1 -n monitoring --overwrite || true
fi

# Alerts
kubectl apply -f ../monitoring/gpu-alerts.yaml

# Kontroler dyskowy
echo "Deploying disk quota controller..."
kubectl apply -f ../quotas/disk-quota-controller.yaml
# Tworzenie ConfigMap dla disk quota controller z pliku
kubectl create configmap disk-quota-script --namespace devops-system \
  --from-file=disk-quota-controller.py=../quotas/disk-quota-controller.py \
  --dry-run=client -o yaml | kubectl apply -f -
# CronJob przypomnień
kubectl apply -f ../reservations/reminder-cronjob.yaml

# RBAC
kubectl apply -f ../rbac/cluster-roles.yaml

echo -e "${GREEN}=== Deployment completed! ===${NC}"
echo ""
echo "API: kubectl port-forward -n devops-system svc/devops-api 8000:8000"
echo "Grafana: kubectl port-forward -n monitoring svc/monitoring-grafana 3000:80 (admin/prom-operator)"
# Start a port-forward only if port 8000 is free. Re-running deploy-all.sh (or the
# test suite, which forwards on its own) otherwise crashes with
# "unable to listen on any of the requested ports: [{8000 8000}]".
if curl -s -m 3 http://localhost:8000/health 2>/dev/null | grep -q '"status":"ok"'; then
    echo "API already reachable on :8000 — reusing existing port-forward."
elif (command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ':8000 ') \
  || (command -v netstat >/dev/null 2>&1 && netstat -ltn 2>/dev/null | grep -q ':8000 '); then
    echo "Port 8000 already in use — skipping port-forward."
else
    kubectl port-forward -n devops-system svc/devops-api 8000:8000 &
fi

# Test suite (set RUN_TESTS=0 to skip). Waits for pods to be ready, then validates the API.
if [ "${RUN_TESTS:-1}" != "0" ]; then
    echo -e "${GREEN}=== Running test suite ===${NC}"
    bash ../../tests_suite/run.sh || echo -e "${RED}Test suite reported failures (see output above).${NC}"
fi
