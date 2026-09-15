# Obraz kontrolera Kopf z zaszytymi zależnościami.
# Jak przy API: bez pip install z internetu przy każdym starcie pod startuje w
# sekundy i nie wpada w CrashLoopBackOff przy wolnej sieci / restartach węzła.
# Kod (task_controller.py) montujemy z ConfigMap controller-scripts, więc zmiany
# w kodzie nie wymagają przebudowy obrazu.
FROM python:3.10-slim

# aiohttp jest potrzebny do wbudowanego endpointu liveness kopf (--liveness).
RUN pip install --no-cache-dir --timeout 60 --retries 5 kopf kubernetes pyyaml aiohttp

WORKDIR /app
# task_controller.py montowane z ConfigMap w /app.
# --liveness wystawia HTTP /healthz (port 8080) — używane przez livenessProbe.
CMD ["kopf", "run", "/app/task_controller.py", "--verbose", "--liveness=http://0.0.0.0:8080/healthz"]
