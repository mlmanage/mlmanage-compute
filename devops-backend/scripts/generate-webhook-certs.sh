#!/bin/bash
set -euo pipefail

NAMESPACE="${NAMESPACE:-devops-system}"
SERVICE_NAME="${SERVICE_NAME:-vram-webhook}"
SECRET_NAME="${SECRET_NAME:-vram-webhook-tls}"
WEBHOOK_NAME="${WEBHOOK_NAME:-vram-limit-webhook}"
FAILURE_POLICY="${VRAM_WEBHOOK_FAILURE_POLICY:-Fail}"

case "$FAILURE_POLICY" in
  Fail|Ignore) ;;
  *) echo "VRAM_WEBHOOK_FAILURE_POLICY must be Fail or Ignore" >&2; exit 2 ;;
esac

CERT_DIR="$(mktemp -d)"
trap 'rm -rf "$CERT_DIR"' EXIT

echo "=== Generating an ephemeral CA and certificate for $WEBHOOK_NAME ==="

openssl genrsa -out "$CERT_DIR/ca.key" 2048
openssl req -x509 -new -nodes -key "$CERT_DIR/ca.key" -sha256 -days 3650 \
  -out "$CERT_DIR/ca.crt" -subj "/CN=VRAM Webhook CA" \
  -addext "keyUsage=critical, keyCertSign, cRLSign" \
  -addext "basicConstraints=critical, CA:TRUE"

openssl genrsa -out "$CERT_DIR/webhook.key" 2048
cat > "$CERT_DIR/webhook.conf" <<EOF
[req]
distinguished_name = req_distinguished_name
req_extensions = v3_req
prompt = no

[req_distinguished_name]
CN = ${SERVICE_NAME}.${NAMESPACE}.svc

[v3_req]
keyUsage = keyEncipherment, dataEncipherment, digitalSignature
extendedKeyUsage = serverAuth
subjectAltName = @alt_names

[alt_names]
DNS.1 = ${SERVICE_NAME}
DNS.2 = ${SERVICE_NAME}.${NAMESPACE}
DNS.3 = ${SERVICE_NAME}.${NAMESPACE}.svc
DNS.4 = ${SERVICE_NAME}.${NAMESPACE}.svc.cluster.local
EOF

openssl req -new -key "$CERT_DIR/webhook.key" -out "$CERT_DIR/webhook.csr" \
  -config "$CERT_DIR/webhook.conf"
openssl x509 -req -in "$CERT_DIR/webhook.csr" \
  -CA "$CERT_DIR/ca.crt" -CAkey "$CERT_DIR/ca.key" -CAcreateserial \
  -out "$CERT_DIR/webhook.crt" -days 365 -sha256 \
  -extensions v3_req -extfile "$CERT_DIR/webhook.conf"

kubectl create secret tls "$SECRET_NAME" \
  --cert="$CERT_DIR/webhook.crt" --key="$CERT_DIR/webhook.key" \
  -n "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

CA_BUNDLE="$(base64 < "$CERT_DIR/ca.crt" | tr -d '\n')"
cat <<EOF | kubectl apply -f -
apiVersion: admissionregistration.k8s.io/v1
kind: MutatingWebhookConfiguration
metadata:
  name: ${WEBHOOK_NAME}
webhooks:
- name: vram.devops.local
  clientConfig:
    service:
      name: ${SERVICE_NAME}
      namespace: ${NAMESPACE}
      path: /mutate
    caBundle: ${CA_BUNDLE}
  rules:
  - operations: ["CREATE"]
    apiGroups: [""]
    apiVersions: ["v1"]
    resources: ["pods"]
  failurePolicy: ${FAILURE_POLICY}
  timeoutSeconds: 5
  # Limit admission to user workload namespaces so an unavailable application webhook cannot
  # block system Pods. VRAM enforcement remains fail-closed for the workloads it governs.
  # This is an unquoted heredoc: avoid shell expansion syntax in comments inside this block.
  namespaceSelector:
    matchLabels:
      devops.dev/user-namespace: "true"
  objectSelector:
    matchExpressions:
    - key: app
      operator: NotIn
      values: [${SERVICE_NAME}]
  admissionReviewVersions: ["v1"]
  sideEffects: None
EOF

echo "Webhook certificate secret and configuration applied (failurePolicy=$FAILURE_POLICY)."
