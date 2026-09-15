#!/bin/bash
# create-local-dev.sh
# Użycie: ./create-local-dev.sh jan
# Obraz: prodimage:1

set -e  # Zatrzymaj skrypt przy pierwszym błędzie

# Funkcja czyszczenia zasobów w przypadku błędu
cleanup_on_error() {
    echo "Wystąpił błąd! Czyszczenie zasobów..."
    if [ -n "$DEV_NAME" ]; then
        kubectl delete namespace "dev-$DEV_NAME" --ignore-not-found >/dev/null 2>&1
        echo "Usunięto namespace dev-$DEV_NAME"
    fi
    exit 1
}

# Ustaw trap dla błędów
trap cleanup_on_error ERR

# Walidacja argumentu
if [ -z "$1" ]; then
    echo "Błąd: Podaj nazwę użytkownika"
    echo "Użycie: $0 <nazwa>"
    exit 1
fi

DEV_NAME=$1

# Sprawdź czy kubectl jest dostępne
if ! command -v kubectl &> /dev/null; then
    echo "Błąd: kubectl nie jest zainstalowane lub nie jest w PATH"
    exit 1
fi

# 1. Sprawdź czy namespace już istnieje
echo "Sprawdzam czy namespace dev-$DEV_NAME już istnieje..."
if kubectl get namespace "dev-$DEV_NAME" >/dev/null 2>&1; then
    echo "Błąd: Namespace dev-$DEV_NAME już istnieje!"
    exit 1
fi

# Stwórz namespace
echo "Tworzę namespace dev-$DEV_NAME..."
kubectl create namespace "dev-$DEV_NAME"

# Funkcja sprawdzająca czy polecenie się powiodło
check_kubectl_success() {
    if [ $? -ne 0 ]; then
        echo "Błąd podczas wykonywania polecenia kubectl!"
        cleanup_on_error
    fi
}

# 2. Stwórz PersistentVolumeClaim (dysk)
echo "Tworzę PersistentVolumeClaim..."
kubectl apply -n "dev-$DEV_NAME" -f "manifests/storage.yaml"
check_kubectl_success

# 3. Stwórz Deployment z Ubuntu
echo "Tworzę Deployment..."
kubectl apply -n "dev-$DEV_NAME" -f "manifests/deployment.yaml"
check_kubectl_success

# 4. Stwórz Service (NodePort dla SSH) z losowym portem 30000-32767
echo "Tworzę Service dla SSH..."
kubectl apply -n "dev-$DEV_NAME" -f "manifests/service.yaml"
check_kubectl_success

# 5. Stwórz NetworkPolicy (izolacja)
echo "Tworzę NetworkPolicy..."
kubectl apply -n "dev-$DEV_NAME" -f "manifests/networkpolicy.yaml"
check_kubectl_success

# 6. Dodaj ConfigMap z konfiguracją (opcjonalnie)
echo "Tworzę ConfigMap z podstawową konfiguracją..."
kubectl apply -n "dev-$DEV_NAME" -f "manifests/configmap.yaml"
check_kubectl_success

# 7. Poczekaj na gotowość poda
echo "Oczekiwanie na gotowość poda..."
if kubectl wait --namespace "dev-$DEV_NAME" \
    --for=condition=ready pod \
    --selector=app=ubuntu \
    --timeout=120s >/dev/null 2>&1; then
    echo "Pod jest gotowy!"
else
    echo "Ostrzeżenie: Pod nie jest gotowy w oczekiwanym czasie. Sprawdź ręcznie:"
    echo "  kubectl get pods -n dev-$DEV_NAME"
fi

# 8. Pobierz adres IP klastra
echo "Pobieram adres IP klastra..."
if command -v minikube &> /dev/null; then
    CLUSTER_IP=$(minikube ip 2>/dev/null || echo "127.0.0.1")
    echo "Używam Minikube IP: $CLUSTER_IP"
else
    # Spróbuj pobrać ExternalIP
    CLUSTER_IP=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="ExternalIP")].address}' 2>/dev/null || true)
    
    if [ -z "$CLUSTER_IP" ]; then
        # Jeśli nie ma ExternalIP, użyj InternalIP
        CLUSTER_IP=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null || true)
    fi
    
    if [ -z "$CLUSTER_IP" ]; then
        # Ostatnia deska ratunku - localhost
        CLUSTER_IP="127.0.0.1"
        echo "Uwaga: Nie można pobrać adresu IP klastra, używam $CLUSTER_IP"
    else
        echo "Używam adresu IP klastra: $CLUSTER_IP"
    fi
fi

# 9. Generuj bezpieczne hasło (opcjonalnie)
# W praktyce lepiej używać kluczy SSH, ale na potrzeby demo:
DEFAULT_PASSWORD="developer"
# Alternatywnie: wygeneruj losowe hasło
# DEFAULT_PASSWORD=$(openssl rand -base64 12 2>/dev/null || echo "developer")
NODE_PORT=$(kubectl get svc ssh -n "dev-$DEV_NAME" -o jsonpath='{.spec.ports[0].nodePort}')
# 10. Wyświetl informacje końcowe
echo ""
echo "========================================"
echo "Środowisko dla $DEV_NAME zostało pomyślnie utworzone!"
echo "========================================"
echo ""
echo "INFORMACJE O DOSTĘPIE:"
echo "  IP:     $CLUSTER_IP"
echo "  Node Port:     $NODE_PORT"
echo "  User:     developer"
echo "  Password: $DEFAULT_PASSWORD"
echo ""
echo "Pełne polecenie SSH:"
echo "  ssh developer@$CLUSTER_IP -p $NODE_PORT"
echo "!!! WAZNE: SSH AKTUALNIE NIE DZIALA I NALEZY KORZYSTAC Z KOMENDY NIZEJ ABY SHELL DO KONTENERA ODPALIC, DZIALA TAK SAMO SSH ALE JEST BEZPIECZNIEJSZE !!!"
echo ""
echo "PODSTAWOWE KOMENDY:"
echo "  Sprawdź status:    kubectl get all -n dev-$DEV_NAME"
echo "  Logi kontenera:    kubectl logs -n dev-$DEV_NAME deployment/ubuntu"
echo "  Shell do kontenera:kubectl exec -n dev-$DEV_NAME -it deployment/ubuntu -- bash"
echo ""
echo "USUWANIE ŚRODOWISKA:"
echo "  kubectl delete namespace dev-$DEV_NAME"
echo ""
echo "MONITOROWANIE:"
echo "  kubectl get pods -n dev-$DEV_NAME -w"
echo "========================================"

# Usuń trap - skrypt zakończył się sukcesem
trap - ERR

exit 0
