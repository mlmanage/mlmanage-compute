#!/usr/bin/env python3
import json
import base64
import logging
import sys
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
import ssl

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        logger.info("%s - %s" % (self.address_string(), format % args))

    def do_POST(self):
        if self.path.split('?')[0] != '/mutate':
            self.send_response(404)
            self.end_headers()
            return

        content_length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(content_length))
        request_uid = body.get('request', {}).get('uid', '')

        if not request_uid:
            logger.error("Missing uid in request")
            self._send_allowed("")
            return

        pod = body.get('request', {}).get('object', {})
        pod_name = pod.get('metadata', {}).get('name', 'unknown')
        pod_namespace = pod.get('metadata', {}).get('namespace', '')
        pod_labels = pod.get('metadata', {}).get('labels', {})

        # Wykluczamy namespace devops-system oraz własnego poda
        if pod_namespace == 'devops-system' or pod_labels.get('app') == 'vram-webhook':
            logger.info(f"Skipping pod {pod_name} in {pod_namespace} (system or self)")
            self._send_allowed(request_uid)
            return

        containers = pod.get('spec', {}).get('containers', [])
        if not containers:
            logger.warning(f"No containers in pod {pod_name} – skipping")
            self._send_allowed(request_uid)
            return

        try:
            if 'env' not in containers[0]:
                containers[0]['env'] = []
            # Preserve a per-task VRAM limit set by the controller; otherwise inject the configured default.
            existing = {e.get('name') for e in containers[0]['env']}
            if 'VRAM_LIMIT_MB' in existing:
                logger.info(f"Pod {pod_name} already has VRAM_LIMIT_MB, leaving as-is")
                self._send_allowed(request_uid)
                return
            default_vram_mb = os.getenv('DEFAULT_VRAM_LIMIT_MB', '4096')
            containers[0]['env'].append({'name': 'VRAM_LIMIT_MB', 'value': default_vram_mb})
            patch = [{'op': 'add', 'path': '/spec/containers/0/env', 'value': containers[0]['env']}]
            patch_bytes = json.dumps(patch).encode()
            response = {
                'apiVersion': 'admission.k8s.io/v1',
                'kind': 'AdmissionReview',
                'response': {
                    'uid': request_uid,
                    'allowed': True,
                    'patch': base64.b64encode(patch_bytes).decode(),
                    'patchType': 'JSONPatch'
                }
            }
            self._send_response(response)
            logger.info(f"Patched pod {pod_name} in {pod_namespace}")
        except Exception as e:
            logger.error(f"Error patching pod {pod_name}: {e}")
            self._send_allowed(request_uid)

    def _send_allowed(self, uid):
        response = {
            'apiVersion': 'admission.k8s.io/v1',
            'kind': 'AdmissionReview',
            'response': {
                'uid': uid,
                'allowed': True
            }
        }
        self._send_response(response)

    def _send_response(self, response):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())

if __name__ == '__main__':
    cert_file = '/etc/webhook/certs/tls.crt'
    key_file = '/etc/webhook/certs/tls.key'
    if not os.path.exists(cert_file) or not os.path.exists(key_file):
        logger.error(f"Certificate or key not found: {cert_file} or {key_file}")
        sys.exit(1)
    try:
        server = HTTPServer(('0.0.0.0', 8443), WebhookHandler)
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(cert_file, key_file)
        server.socket = ssl_context.wrap_socket(server.socket, server_side=True)
        logger.info("VRAM webhook started on port 8443 with TLS")
        server.serve_forever()
    except Exception as e:
        logger.error(f"Failed to start server: {e}")
        sys.exit(1)
