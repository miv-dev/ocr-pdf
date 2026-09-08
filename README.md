# Deploying the OCR service

Layout on the server:

```
/srv/occular/
├── index.html            # served by nginx, not by Flask
├── server.py
├── deploy/
│   ├── wsgi.py
│   ├── gunicorn.conf.py
│   ├── nginx.conf
│   └── occular.service
└── .venv/
/var/lib/occular/         # model weights cache (HOME of the service user)
/run/occular/             # gunicorn socket, created by systemd
```

## 1. User and directories

```bash
sudo useradd --system --home /var/lib/occular --shell /usr/sbin/nologin occular
sudo mkdir -p /srv/occular /var/lib/occular
sudo chown -R occular:www-data /srv/occular /var/lib/occular
```

## 2. Virtualenv

```bash
cd /srv/occular
sudo -u occular python3 -m venv .venv
sudo -u occular .venv/bin/pip install occular-ocr flask gunicorn opencv-python-headless
```

Use `opencv-python-headless`, not `opencv-python` — the GUI build drags in X11
libraries that a server does not have.

## 3. Warm the model cache once

Weights download on first use. Do it as the service user so they land in the
right cache and the first real request isn't a five-minute wait:

```bash
sudo -u occular HOME=/var/lib/occular .venv/bin/python -c \
  "from occular import ocr, download_reading_order; download_reading_order()"
```

## 4. Service

```bash
sudo cp deploy/occular.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now occular
sudo systemctl status occular
```

## 5. Nginx

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/occular
sudo ln -s /etc/nginx/sites-available/occular /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d ocr.example.com
```

Replace `ocr.example.com` in the config first.

## Sizing

Every worker holds its own copy of the models (roughly 1 GB) and every request
pins a CPU for several seconds. Start with:

| Cores | RAM   | `WEB_CONCURRENCY` | `OMP_NUM_THREADS` |
| ----- | ----- | ----------------- | ----------------- |
| 4     | 8 GB  | 1                 | 4                 |
| 8     | 16 GB | 2                 | 4                 |
| 16    | 32 GB | 4                 | 4                 |

`WEB_CONCURRENCY × OMP_NUM_THREADS` should stay at or below the core count.
Both are set in `occular.service`; edit and `systemctl daemon-reload`.

## Things worth adding before this faces the open internet

- **Access control.** Anyone who can reach `/ocr` can spend your CPU. The
  nginx rate limit slows abuse but doesn't stop it — put the site behind basic
  auth, an allowlist, or a login if it isn't public by design.
- **Queueing.** Under concurrent load requests just pile up against a 300 s
  timeout. If more than a handful of people use it, move recognition to a job
  queue (RQ or Celery) and have the browser poll for the result.
- **Privacy.** The browser now uploads page images to your server, which the
  local version never did. Nothing is written to disk in `server.py`, but the
  images do pass through nginx's buffers and appear in no logs by default —
  worth stating plainly on the page if other people will use it.
