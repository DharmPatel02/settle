# Datasources and dashboards are provisioned from files in Git. Rebuild after editing:
# docker compose up -d --build grafana
FROM grafana/grafana:12.1.0
COPY provisioning /etc/grafana/provisioning
COPY dashboards /var/lib/grafana/dashboards
