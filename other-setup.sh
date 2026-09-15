#!/bin/bash
# Konfiguracja hosta po (opcjonalnym) restarcie: Docker, (opcjonalnie) NVIDIA toolkit,
# minikube/kubectl/helm i klaster. Wykrywa brak GPU i pomija wszystkie kroki GPU.
set -u

echo "Kontynuacja instalacji..."

# --- Wybór środowiska: dummy (minikube) vs professional (klaster k8s) ---
# Bez pytania: MLM_ENV=dummy|professional ./other-setup.sh
MLM_ENV="${MLM_ENV:-}"
if [ -z "$MLM_ENV" ]; then
    echo ""
    echo "Wybierz typ środowiska:"
    echo "  1) dummy        - lokalne demo/dev: zainstaluj i uruchom minikube (jednowęzłowe)"
    echo "  2) professional - prawdziwy serwer/klaster (k3s/kubeadm/managed)"
    read -r -p "Podaj 1 lub 2 [1]: " _envchoice
    case "$_envchoice" in
        2|professional|prof) MLM_ENV="professional" ;;
        *) MLM_ENV="dummy" ;;
    esac
fi
echo "Wybrane środowisko: $MLM_ENV"

# --- Wykrywanie GPU: jeśli brak, pomijamy WSZYSTKIE kroki GPU ---
# Wymuszenie: HAS_GPU=1/0 ./other-setup.sh
HAS_GPU="${HAS_GPU:-}"
if [ -z "$HAS_GPU" ]; then
    if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
        HAS_GPU=1
    else
        HAS_GPU=0
    fi
fi
if [ "$HAS_GPU" = "1" ]; then
    echo "Wykryto GPU NVIDIA:"; nvidia-smi -L 2>/dev/null || nvidia-smi 2>/dev/null
else
    echo "Brak GPU NVIDIA (lub brak sterowników) — pomijam wszystkie kroki GPU (CPU-only)."
fi

# ============================================================================
# 2. Docker
# ============================================================================
echo "Instalowanie Dockera..."
for pkg in docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc; do sudo apt-get remove -y $pkg 2>/dev/null || true; done
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Dodanie użytkownika do grupy docker (działa w nowych sesjach; poniżej dla bieżącej używamy `sg docker`).
sudo usermod -aG docker "$USER"
sudo systemctl enable --now docker 2>/dev/null || true

# Pomocnik: uruchom polecenie z aktywną grupą docker w bieżącej sesji (bez wylogowania).
# Jeśli użytkownik już ma aktywną grupę docker, uruchamiamy wprost.
drun() {
    if id -nG 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
        "$@"
    else
        sg docker -c "$*"
    fi
}

# ============================================================================
# 3. NVIDIA Container Toolkit — TYLKO gdy jest GPU
# ============================================================================
if [ "$HAS_GPU" = "1" ]; then
    echo "Instalowanie NVIDIA Container Toolkit..."
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
      sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
      sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
    sudo apt-get update
    sudo apt-get install -y nvidia-container-toolkit
    sudo nvidia-ctk runtime configure --runtime=docker
    sudo systemctl restart docker
else
    echo "Pomijam NVIDIA Container Toolkit (brak GPU)."
fi

# ============================================================================
# 4-6. minikube / kubectl / helm
# ============================================================================
echo "Instalowanie Minikube..."
curl -LO https://github.com/kubernetes/minikube/releases/latest/download/minikube-linux-amd64
sudo install minikube-linux-amd64 /usr/local/bin/minikube

echo "Instalowanie kubectl..."
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl

echo "Instalowanie Helm..."
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

# ============================================================================
# 7. Klaster Kubernetes
# ============================================================================
# Flaga GPU dla minikube tylko gdy jest GPU.
MK_GPU=""; [ "$HAS_GPU" = "1" ] && MK_GPU="--gpus=all"

if [ "$MLM_ENV" = "dummy" ]; then
    echo "Uruchamianie Minikube (środowisko dummy) ${MK_GPU:-(CPU-only)}..."
    drun minikube start $MK_GPU
    echo "Włączanie wbudowanego rejestru obrazów (addon minikube)..."
    drun minikube addons enable registry
else
    echo "Środowisko professional: automatyczne wdrożenie klastra (k3s) + rejestru."
    if kubectl cluster-info >/dev/null 2>&1; then
        echo "Wykryto istniejący klaster Kubernetes — używam go."
    else
        echo "Brak klastra — instaluję k3s..."
        curl -sfL https://get.k3s.io | sh -s - --write-kubeconfig-mode 644
        export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
        mkdir -p "$HOME/.kube" && sudo cp /etc/rancher/k3s/k3s.yaml "$HOME/.kube/config" 2>/dev/null && sudo chown "$(id -u):$(id -g)" "$HOME/.kube/config" 2>/dev/null || true
        for ((i=0; i<30; i++)); do kubectl get nodes 2>/dev/null | grep -q " Ready" && break; sleep 5; done
        echo "k3s zainstalowany i gotowy."
        # device-plugin tylko gdy jest GPU.
        if [ "$HAS_GPU" = "1" ]; then
            kubectl apply -f https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.16.2/deployments/static/nvidia-device-plugin.yml 2>/dev/null || true
        fi
    fi

    # KLUCZOWE dla k3s: manifest device-plugina od NVIDIA nie ustawia runtimeClassName, bo
    # zakłada, że nvidia jest DOMYŚLNYM runtime containerd. W k3s tak NIE jest — k3s tworzy
    # osobną RuntimeClass "nvidia" (gdy wykryje nvidia-container-runtime), a pody muszą o nią
    # poprosić jawnie. Bez tego device-plugin startuje, ale nie widzi karty i loguje:
    #   "Incompatible strategy detected auto" / "No devices found. Waiting indefinitely."
    # Skutek: węzeł NIE ogłasza nvidia.com/gpu, więc /gpu/capabilities jest puste, zadania GPU
    # nie mają się gdzie uruchomić, a testy partycjonowania GPU są po cichu pomijane.
    # Ustawiamy to idempotentnie także na ISTNIEJĄCYM klastrze (nie tylko po świeżej instalacji).
    if [ "$HAS_GPU" = "1" ] && kubectl get ds -n kube-system nvidia-device-plugin-daemonset >/dev/null 2>&1; then
        if kubectl get runtimeclass nvidia >/dev/null 2>&1; then
            current_rc="$(kubectl get ds -n kube-system nvidia-device-plugin-daemonset \
                -o jsonpath='{.spec.template.spec.runtimeClassName}' 2>/dev/null || true)"
            if [ "$current_rc" != "nvidia" ]; then
                echo "Ustawiam runtimeClassName=nvidia na device-pluginie (wymagane w k3s)..."
                kubectl patch daemonset nvidia-device-plugin-daemonset -n kube-system --type=merge \
                  -p '{"spec":{"template":{"spec":{"runtimeClassName":"nvidia"}}}}' >/dev/null 2>&1 || true
                kubectl -n kube-system rollout status daemonset/nvidia-device-plugin-daemonset --timeout=180s || true
            else
                echo "device-plugin już używa runtimeClassName=nvidia."
            fi
        else
            echo "UWAGA: brak RuntimeClass 'nvidia' — k3s nie wykrył nvidia-container-runtime."
            echo "       Zainstaluj NVIDIA Container Toolkit i zrestartuj k3s, inaczej węzeł nie ogłosi nvidia.com/gpu."
        fi
    fi
    echo "Wdrażanie wewnętrznego rejestru obrazów (registry:2)..."
    # Store registry data on a PVC so image layers survive Pod and node restarts.
    REGISTRY_PVC_SIZE="${REGISTRY_PVC_SIZE:-20Gi}"
    kubectl apply -f - <<REGYAML
apiVersion: v1
kind: Namespace
metadata: { name: registry }
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: registry-data, namespace: registry }
spec:
  accessModes: ["ReadWriteOnce"]
  resources: { requests: { storage: ${REGISTRY_PVC_SIZE} } }
---
apiVersion: apps/v1
kind: Deployment
metadata: { name: registry, namespace: registry }
spec:
  replicas: 1
  strategy: { type: Recreate }
  selector: { matchLabels: { app: registry } }
  template:
    metadata: { labels: { app: registry } }
    spec:
      containers:
      - name: registry
        image: registry:2
        ports: [ { containerPort: 5000 } ]
        volumeMounts: [ { name: data, mountPath: /var/lib/registry } ]
      volumes:
      - name: data
        persistentVolumeClaim: { claimName: registry-data }
---
apiVersion: v1
kind: Service
metadata: { name: registry, namespace: registry }
spec:
  type: NodePort
  selector: { app: registry }
  ports: [ { port: 80, targetPort: 5000, nodePort: 32000 } ]
REGYAML
    # Świadomie NIE ignorujemy tu błędu: bez działającego rejestru cały deploy professional
    # jest bezcelowy (obrazy nie dadzą się pobrać), więc lepiej powiedzieć to wprost.
    if ! kubectl -n registry rollout status deployment/registry --timeout=180s; then
        echo "BŁĄD: rejestr nie wstał. Najczęstsza przyczyna: PVC registry-data nie jest Bound."
        kubectl -n registry get pvc registry-data 2>/dev/null || true
        kubectl -n registry describe pvc registry-data 2>/dev/null | tail -15 || true
        echo "Sprawdź, czy klaster ma domyślną StorageClass:  kubectl get sc"
    fi
    echo "Rejestr: registry.registry.svc.cluster.local:80 (NodePort 32000)."

    # Aby węzły (containerd k3s) mogły POBIERAĆ obrazy z wewnętrznego rejestru po HTTP
    # (localhost:32000), adres ten musi być oznaczony jako rejestr insecure. Bez tego
    # pull obrazów systemowych (devops-api/-controller) oraz obrazów zadań użytkownika
    # kończy się błędem TLS (ImagePullBackOff) i wymaga ręcznej konfiguracji. Robimy to
    # automatycznie — to samo, co addon `registry` załatwia w minikube (tryb dummy).
    if command -v k3s >/dev/null 2>&1 || [ -f /etc/rancher/k3s/k3s.yaml ]; then
        echo "Konfiguruję k3s (containerd): localhost:32000 jako rejestr insecure (HTTP)..."
        sudo mkdir -p /etc/rancher/k3s
        sudo tee /etc/rancher/k3s/registries.yaml >/dev/null <<'REGCFG'
mirrors:
  "localhost:32000":
    endpoint:
      - "http://localhost:32000"
REGCFG
        sudo systemctl restart k3s 2>/dev/null || true
        # Czekaj aż węzeł znów będzie Ready po restarcie k3s.
        for ((i=0; i<30; i++)); do kubectl get nodes 2>/dev/null | grep -q " Ready" && break; sleep 3; done
    else
        echo "UWAGA: klaster nie jest k3s — jeśli używa containerd, oznacz ręcznie 'localhost:32000'"
        echo "       jako rejestr insecure (HTTP), aby węzły mogły pobierać obrazy z wewnętrznego rejestru."
    fi
fi

# ============================================================================
# 8. Repo Helm NVIDIA (GPU Operator instaluje deploy-all.sh) — tylko gdy GPU
# ============================================================================
if [ "$HAS_GPU" = "1" ]; then
    echo "Dodawanie repo Helm NVIDIA..."
    helm repo add nvidia https://helm.ngc.nvidia.com/nvidia 2>/dev/null || true
    helm repo update 2>/dev/null || true
else
    echo "Pomijam repo NVIDIA GPU Operator (brak GPU)."
fi

echo "=========================================="
echo "Konfiguracja zakończona pomyślnie! (GPU: $([ "$HAS_GPU" = "1" ] && echo tak || echo nie), env: $MLM_ENV)"
echo "=========================================="
if ! id -nG 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
    echo "WAŻNE: dodano Cię do grupy 'docker', ale bieżąca powłoka jeszcze jej nie ma."
    echo "       Uruchom:  newgrp docker     (lub wyloguj się i zaloguj ponownie) przed deploy-all.sh"
fi
echo "Następnie:  cd devops-backend/scripts && MLM_ENV=$MLM_ENV ./deploy-all.sh"
