# GLPI Builder v11 for Synology

GLPI Builder creates and restores internal GLPI Docker projects on a
Synology NAS. This version was developed for a Synology RS822RP+ with projects
stored below `/volume1/docker`. The proven v7 structure of every generated
`docker-compose.yml` is intentionally unchanged and protected by regression
tests and SHA-256 checks.

## Security warning

The Builder receives access to `/var/run/docker.sock` and can therefore
effectively administer the Docker host. It requires a single administrator
login with a strong PBKDF2 password hash and RFC 6238 TOTP MFA, and refuses to
serve management routes when that configuration is missing or invalid. Login
is an additional control, not a reason to expose port `5055` to the internet.

- `127.0.0.1` makes the Builder reachable only from the NAS itself.
- To access it from an administration PC, bind it to the fixed internal IP
  address of the NAS.
- Restrict port `5055` in the DSM firewall to the administration PC or
  management VLAN.
- Prefer an SSH tunnel when the Builder does not need permanent LAN access.
- `0.0.0.0` listens on every NAS interface and is therefore discouraged.

Use HTTPS through Synology Reverse Proxy whenever the Builder is reachable over
the network, keep `BUILDER_SESSION_COOKIE_SECURE=true`, and restrict the port to
administrator devices or a management VLAN. The login session expires after 15
minutes of inactivity and after eight hours in all cases. Repeated failures are
rate limited per source address and globally.

## Features

- English dashboard for managed GLPI projects plus read-only discovery and
  rejection reasons for existing GLPI Compose projects;
- strict GLPI project discovery: unrelated Docker directories and generic
  `.env` files are excluded;
- guided full-restore and fresh-installation flow;
- selection of locally available and allowed GLPI and database images;
- automatic suggestion of the first free GLPI web port starting at `8775`;
- rejection of ports used by running or stopped containers or reserved in any
  other project `.env`;
- mandatory database and GLPI files backups for a full restore;
- optional restore without plugins, marketplace data, or plugin cache;
- server-side preflight and an execution plan that must be confirmed within ten
  minutes;
- revalidation of images, backups, port, and project state immediately before
  execution;
- live progress, elapsed time, activity timeline, and a complete project log;
- GLPI web-port and cookie-setting management;
- a managed 32 KiB Apache request-URL limit that is reapplied whenever a GLPI
  project is created or rebuilt;
- GLPI container reapplication, database check, and diagnostics;
- only one mutating administration action at a time;
- central Synology backup script with locking, atomic publication, checksums,
  and 60-day retention by default;
- backup readiness under **Project management**, with actionable warnings,
  latest successful backup time, size, and checksum-manifest status;
- **Run backup now** as a monitored background job using the same script as
  Synology Task Scheduler;
- one shared Docker inventory per dashboard render with a five-second cache
  that is invalidated immediately before and after administration actions;
- one-pass discovery of database and GLPI files backups;
- production Gunicorn server with one process and four request threads;
- health check, security headers, CSRF protection, and disabled browser caching.

## Requirements

- Synology DSM with Container Manager/Docker and a working Docker CLI;
- SSH access to the NAS and an account permitted to use `sudo`;
- project storage below `/volume1/docker`;
- backups below `/volume1/docker/_BACKUPS`;
- the required GLPI and MariaDB/MySQL images must already exist locally on the
  NAS and match the allowed prefixes in `.env`;
- a free TCP port for the Builder, `5055` by default.

Pull missing images on the NAS before using the Builder, for example:

```sh
sudo docker pull glpi/glpi:<tag>
sudo docker pull mariadb:<tag>
```

## Installation

Extract the release so that this directory exists:

```text
/volume1/docker/glpi-builder
```

Then connect over SSH and enter the directory:

```sh
cd /volume1/docker/glpi-builder
```

### Option A: local access on the NAS only

Run the installer. It creates the configuration and internal session key,
then starts the administrator, bind-IP, password, and OTP setup wizard before
building and starting the container:

```sh
sudo sh install_on_synology.sh
```

The provisioning helper prompts twice for a password of at least 14 characters,
first asks which IPv4 address Docker should bind to, shows a Base32 secret and
`otpauth://` URI, and requires a current authenticator code before enabling the
account. Press Enter to keep the recommended `127.0.0.1` loopback default. It
stores only the bind address, password hash, and TOTP secret in `.env`; the
plaintext password is never written.

Use an SSH tunnel from a PC:

```sh
ssh -L 5055:127.0.0.1:5055 user@NAS-IP
```

Then open `http://127.0.0.1:5055` on that PC.

### Option B: access from an administration PC on the LAN

Run the installer from option A and enter the fixed internal
IPv4 address of the NAS when the provisioning wizard asks for the bind address:

```sh
sudo sh install_on_synology.sh
```

The resulting configuration contains, for example:

```env
BUILDER_BIND_IP=192.168.1.50
BUILDER_PORT=5055
```

Replace `192.168.1.50` with the fixed internal IP address of the NAS and open
`http://192.168.1.50:5055` from the administration PC.

Avoid `0.0.0.0` unless every IPv4 interface must listen. The interactive wizard
requires typing `EXPOSE` for that value; non-interactive provisioning requires
both `--bind-ip 0.0.0.0` and `--allow-all-interfaces`.

The installer:

1. creates `.env` and a stable session key when needed;
2. starts the bind-IP, administrator, password, and OTP wizard when secure
   credentials are not yet configured;
3. validates the session key, password hash, TOTP secret, timeouts, and mode
   600 on `.env`;
4. installs the managed Synology backup script;
5. builds the `glpi-builder:latest` image;
6. verifies the locked YAML contract and static security checks;
7. preserves an existing Builder container under a timestamped name;
8. starts the new `glpi-builder` as a Compose project, so Synology Container
   Manager shows its live state and provides working Start/Stop controls;
9. leaves the previous container stopped if the health check fails, preventing
   rollback to an older unauthenticated management interface.

To replace the administrator password or TOTP enrollment, stop the Builder,
run the provisioning helper again, and rerun the installer. The previous
credentials stop working when the new container starts.

For upgrades, keep the existing `.env` file. Do not copy `.env.example` over
it: that would discard the bind choice and authentication enrollment. Duplicate
configuration keys are rejected before Docker is allowed to publish a port.

## Routine administration

Check status and published port:

```sh
sudo docker ps --filter name=glpi-builder
sudo docker port glpi-builder
```

View logs:

```sh
sudo docker logs --tail 100 glpi-builder
```

Stop and start the Builder:

```sh
sudo docker compose --project-name glpi-builder --env-file .env -f docker-compose.app.yml stop
sudo docker compose --project-name glpi-builder --env-file .env -f docker-compose.app.yml start
```

Test the health endpoint inside the container:

```sh
sudo docker exec glpi-builder python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/healthz').read().decode())"
```

After changing `.env`, run the installer again to replace the active container.
A Docker container does not automatically inherit changed environment values.

## Creating or restoring a project

1. Open the Builder and review any discovered projects.
2. Enter a valid project name and the desired GLPI web port.
3. Select locally available GLPI and database images.
4. Choose **Full restore** or **Fresh installation**.
5. For **Full restore**, select both a database backup and a GLPI
   files/configuration backup.
6. Under **Advanced settings**, verify the time zone, cookies, overwrite
   confirmation, and scheduled backup source.
7. Select **Review plan**, inspect the execution plan, and confirm it within ten
   minutes.
8. Keep the progress page open until the operation has fully completed.

An existing project name is overwritten only when **Overwrite existing
project** is enabled and the project name is entered exactly as confirmation.

The **Projects** dashboard lists validated GLPI projects. A separate
**Detected but not managed** section also shows GLPI Compose projects found via
Docker labels and allowed images, including their live status, published port,
images, and the exact Builder contract checks that failed. These detected
projects are read-only: the Builder does not rewrite their Compose or `.env`
files and does not expose management actions for them.

Read-only discovery also recognizes older GLPI containers whose original image
tag has become dangling (shown by Synology as `glpi/glpi:<none>`) when GLPI-
specific environment or mount data is still present. Database names using
`<project>-db-<suffix>` are paired with the corresponding GLPI project as well.

Managed discovery requires a matching `PROJECT_NAME`, complete GLPI/database environment values,
allowed GLPI and database image prefixes, internal port `8080`, the persistent
`db`, `glpi`, and `plugins` directories, and the expected GLPI/database
containers and Synology volume paths in `docker-compose.yml`. Other Docker
containers and unrelated folders with an `.env` are ignored.

Supported database backups:

- `.sql`, `.sql.gz`, `.dump`, and `.dump.gz`.

The GLPI files/config backup selector shows only `.tar.gz` archives, matching
the format created by the managed GLPI backup script. Other archive formats
are not offered in the interface.

The restore engine can still process these GLPI files backup formats when a
path is supplied by an existing saved plan or compatible integration:

- a directory below `BACKUP_ROOT`;
- `.zip`, `.tar`, `.tar.gz`, `.tgz`, `.tar.bz2`, `.tbz2`, `.tar.xz`, and `.txz`.

Every selected backup must be below the fixed `BACKUP_ROOT`. Symlinks and paths
outside that directory are rejected.

The database and GLPI files selectors show only backups whose relative path
contains `glpi` (case-insensitive), including the managed `GLPI_Backup_*`
folders. Backups for other applications may remain below `_BACKUPS`, but are
not offered by GLPI Builder.

## Scheduled Synology backup

The installer automatically copies the bundled script to:

```text
/volume1/docker/_BACKUPS/Restore_Scripts/GLPI/GLPI_backup.sh
```

On the first installation, an existing unmanaged `GLPI_backup.sh` is preserved
once as `GLPI_backup.pre-builder.sh`. The managed script receives mode `750`.

For an existing project, select **Use for scheduled backups**. For a new
restore, leave **Use this project for scheduled backups** enabled. The Builder
then atomically writes:

```text
/volume1/docker/_BACKUPS/Restore_Scripts/GLPI/GLPI_backup.env
```

This file receives mode `600` and contains the current project path, database
container, database name, and retention period. Only one project can be the
active scheduled backup source at a time.

The selected project receives a green **Backup source** badge above its web
port. Under **Project management > Scheduled backup**, it shows **Backup ready**
only when the managed script, backup environment, MariaDB credential file, GLPI
data directories, and running database container are all present. Otherwise it
shows **Needs attention** with the exact missing requirements.

The Builder does not create or modify `GLPI_mysql_backup.cnf`. Ensure that this
file already exists:

```text
/volume1/docker/_BACKUPS/Restore_Scripts/GLPI/GLPI_mysql_backup.cnf
```

Minimal example:

```ini
[client]
user=root
password=<database-root-password>
```

Restrict access to this file:

```sh
sudo chmod 600 /volume1/docker/_BACKUPS/Restore_Scripts/GLPI/GLPI_mysql_backup.cnf
```

Create one task in Synology Task Scheduler with this fixed command:

```sh
/bin/bash /volume1/docker/_BACKUPS/Restore_Scripts/GLPI/GLPI_backup.sh
```

When **Project management > Scheduled backup** reports **Backup ready**, **Run
backup now** starts this same script as a monitored background task. The
progress page shows the current phase, output, completion state, and action log.
Manual backups use the same global administration lock as restores, so two
mutating operations cannot run at the same time.

Each run creates a `GLPI_Backup_YYYYMMDD-HHMMSS` directory containing:

- `glpi-database.sql`;
- `glpi-files.tar.gz`;
- `BACKUP_INFO`, which identifies the source project and creation time;
- `SHA256SUMS` when `sha256sum` or `shasum` is available.

Sessions, cache, and temporary GLPI files are excluded. The script prevents
concurrent runs, publishes a backup only after full success, and by default
removes only `GLPI_Backup_*` directories older than 60 days.

The dashboard attributes a backup to a project only when `BACKUP_INFO` matches
that project. Backups created by older script versions remain usable but are not
shown as the latest verified project backup until a new backup has completed.

## Rollback

After a successful installation, restore the Builder container preserved
immediately before it with:

```sh
cd /volume1/docker/glpi-builder
sudo sh rollback_on_synology.sh
```

Rollback first starts the previous image as an isolated candidate with no
published port. It continues only when health succeeds, unauthenticated access
is denied, and `/login` is available. The current authenticated container is
not stopped until that proof passes and is preserved for automatic recovery if
the published rollback fails. Rollback does not reverse GLPI project changes,
recover deleted data, or restore the backup script or `.env`.

## Configuration reference

| Variable | Default | Purpose |
| --- | --- | --- |
| `BUILDER_BIND_IP` | `127.0.0.1` | NAS address on which the Builder is published |
| `BUILDER_PORT` | `5055` | Builder TCP port on the NAS |
| `TZ` | `Europe/Brussels` | Time zone for the Builder and projects |
| `FLASK_SECRET_KEY` | generated automatically | Internal session and CSRF key; no manual action required |
| `BUILDER_ADMIN_USERNAME` | none | Required single administrator username |
| `BUILDER_ADMIN_PASSWORD_HASH` | none | Required PBKDF2-SHA256 hash created by the provisioning helper |
| `BUILDER_ADMIN_TOTP_SECRET` | none | Required 160-bit-or-stronger Base32 TOTP secret |
| `BUILDER_SESSION_COOKIE_SECURE` | `true` | Send the login cookie only over HTTPS; use `false` only for loopback HTTP/SSH-tunnel testing |
| `BUILDER_SESSION_TIMEOUT_SECONDS` | `900` | Idle session timeout, allowed range 300-86400 seconds |
| `BUILDER_SESSION_ABSOLUTE_TIMEOUT_SECONDS` | `28800` | Absolute session timeout, at least the idle timeout |
| `BUILDER_AUTH_STATE_PATH` | `/volume1/docker/.glpi-builder-auth-state` | Persistent mode-600 TOTP replay counter |
| `ALLOWED_GLPI_IMAGES` | `glpi/glpi:` | Comma-separated allowed image prefixes |
| `ALLOWED_DB_IMAGES` | `mariadb:,mysql:` | Comma-separated allowed database-image prefixes |
| `MAX_SCAN_ENTRIES` | `500` | Maximum number of backup choices shown; traversal is bounded to protect responsiveness |

## Tests and development

Run the complete quality loop with:

```sh
sh scripts/dev_loop.sh
```

It checks Python and shell syntax, the exact YAML contract, Compose
configuration, a clean Docker build, automatically discovered functional tests,
installed dependencies, and a real container health check.
See [DEVELOPMENT_LOOP.md](DEVELOPMENT_LOOP.md) for the exact checks and release
rules.

## Operational recommendations

- Keep at least one tested backup outside the same NAS.
- Monitor free disk space and rotate container logs.
- Test large restores in a separate project on a free port first.
- Do not upgrade MariaDB/MySQL across major versions without a prior dump and
  restore test.
- After a restore, verify the GLPI version, database connection, plugins,
  scheduled tasks, email, and authentication before using the project in
  production.
