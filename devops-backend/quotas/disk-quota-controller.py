#!/usr/bin/env python3
import kubernetes
import subprocess
import time
import logging
import requests
from prometheus_client import Gauge, start_http_server
from kubernetes import client, config
config.load_incluster_config()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

disk_usage_percent = Gauge('user_disk_usage_percent', 'Disk usage percentage', ['user', 'volume_type'])

def check_all_user_pvcs():
    v1 = kubernetes.client.CoreV1Api()
    namespaces = v1.list_namespace(label_selector="devops.dev/user-namespace=true")
    for ns in namespaces.items:
        user_name = ns.metadata.labels.get('devops.dev/user', 'unknown')
        pvcs = v1.list_namespaced_persistent_volume_claim(ns.metadata.name)
        for pvc in pvcs.items:
            volume_type = "unknown"
            if "home" in pvc.metadata.name:
                volume_type = "home"
            elif "scratch" in pvc.metadata.name:
                volume_type = "scratch"
            elif "project" in pvc.metadata.name:
                volume_type = "project"
            logger.info(f"USER {user_name}: {volume_type} PVC {pvc.metadata.name} found (size: {pvc.spec.resources.requests.get('storage', 'unknown')})")

if __name__ == "__main__":
    start_http_server(9090)
    while True:
        check_all_user_pvcs()
        time.sleep(300)
