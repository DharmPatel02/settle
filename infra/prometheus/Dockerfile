# Config is baked into the image instead of bind-mounted, so it works even where Docker cannot
# mount the repository folder. Rebuild after editing: docker compose up -d --build prometheus
FROM prom/prometheus:v3.5.0
COPY prometheus.yml alerts.yml /etc/prometheus/
