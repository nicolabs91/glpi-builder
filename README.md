# Docker App Manager 0.5.0-rc.1 for Synology

Docker Application Manager creates and manages supported internal Docker
applications on a Synology NAS. Existing GLPI Builder projects remain fully
compatible and retain their proven create, restore and backup paths. This
version was developed for a Synology RS822RP+ with projects
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

- an extensible application-profile catalog alongside the unchanged legacy
  GLPI project contract;
- fresh deployments of n8n with PostgreSQL and Team Password Manager with
  the vendor-documented MySQL 5.7 database, each using an isolated network
  and persistent bind mounts;
- isolated Team Password Manager `.sql` and `.sql.gz` test restores into a
  new database on an internal Docker network without an external route;
- optional checksum-backed TPM backup-set manifests, uploads archives,
  post-restore proof reports, expiry and recoverable quarantine archiving
  (see `docs/TPM_QUARANTINE_RESTORE.md`);
- read-only NAS preflight checks for architecture, writable storage, free
  space, Docker and Compose, followed by image/platform proof before writing a
  project;
- exact backup/image version matching and fail-closed SQL validation before a
  test restore is allowed to start;
- backup intervals shown as days, weeks and months instead of raw hours;
- explicit `.builder-app.json` ownership manifests, so arbitrary Compose
  projects are never silently adopted or mutated;
- profile-specific image allowlists, health checks and recovery guidance;
- generated secrets kept only in a mode-600 `.env`; sanitized Compose output
  exposes placeholders rather than credential values;

- professional multi-page console with Overview, Applications, Backups, Activity,
  Settings, project details, and a guided create/restore wizard;
- read-only discovery and rejection reasons for existing GLPI Compose projects;
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
- independent scheduled-backup settings per project, executed serially by one
  central Synology Task Scheduler dispatcher;
- backup scripts with locking, atomic publication, checksums, project-specific
  storage, and configurable retention;
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
- SSH access and an account permitted to use `sudo` for the classic installer,
  or Synology Container Manager and File Station for the graphical installer;
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
/volume1/docker/docker-app-manager
```

Then connect over SSH and enter the directory:

```sh
cd /volume1/docker/docker-app-manager
```

### Rename an existing GLPI Builder installation

Version 0.4.1 changes the Synology directory, Compose project, service,
container, image and authentication replay-state name from `glpi-builder` to
`docker-app-manager`. Existing managed GLPI application directories are not
renamed or modified.

For the safest migration, extract the new release into
`/volume1/docker/docker-app-manager` while keeping the existing
`/volume1/docker/glpi-builder` directory, then run:

```sh
sudo sh /volume1/docker/docker-app-manager/migrate_to_docker_app_manager.sh
```

The migration refuses to merge existing configuration in the new directory.
It stops the legacy Builder, copies `.env`, `config` and the TOTP replay state,
starts the new Compose project, and requires the normal authenticated health
check to pass. Keep `/volume1/docker/glpi-builder` and the stopped pre-upgrade
container until login, application discovery and backup-source verification
have all succeeded. The old Container Manager project can be removed only
after that verification; do not select an option that deletes project files.

If SSH is unavailable, stop the old project, make a backup of its `config`
directory, create `/volume1/docker/docker-app-manager` from the new release,
copy the old `config` directory into it, and create a new Container Manager
project from `docker-compose.container-manager.yml`. The SSH migration is
preferred because it also preserves `.env`, the replay counter and a tested
container rollback path.

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
5. builds the `docker-app-manager:latest` image;
6. verifies the locked YAML contract and static security checks;
7. preserves an existing Builder container under a timestamped name;
8. starts the new `docker-app-manager` as a Compose project, so Synology Container
   Manager shows its live state and provides working Start/Stop controls;
9. leaves the previous container stopped if the health check fails, preventing
   rollback to an older unauthenticated management interface.

To replace the administrator password or TOTP enrollment, stop the Builder,
run the provisioning helper again, and rerun the installer. The previous
credentials stop working when the new container starts.

For upgrades, keep the existing `.env` file. Do not copy `.env.example` over
it: that would discard the bind choice and authentication enrollment. Duplicate
configuration keys are rejected before Docker is allowed to publish a port.

### Installation without SSH in Synology Container Manager

The release also includes `docker-compose.container-manager.yml` for a fully
graphical first installation:

1. Download the release ZIP and extract its `docker-app-manager` directory to
   `/volume1/docker/docker-app-manager` with File Station.
2. Open **Container Manager → Project → Create**.
3. Select `/volume1/docker/docker-app-manager` as the project path and choose
   `docker-compose.container-manager.yml`.
4. Review the project. By default port `5055` is published on every NAS IPv4
   interface. Restrict this port in DSM Firewall to the administration PC or
   management VLAN before starting the project.
5. Build and start the project. Open the `docker-app-manager` container log in
   Container Manager and copy the one-time setup token.
6. Open `http://NAS-IP:5055/setup`, enter the setup token, create the
   administrator password, add the displayed Base32 secret to an authenticator
   app, and confirm the current six-digit code.
7. Follow the optional **Scheduled backups** step. It shows the exact
   user-defined DSM Task Scheduler command, the required root/five-minute
   schedule, a copy button, and live task-detection status. This step can be
   completed later from **Backups** without weakening account setup.

The random setup token is printed only in the container log and changes after
an unconfigured restart, preventing another LAN client from claiming the first
administrator account. The setup page exposes no Docker management actions. It stores only a PBKDF2
password hash, TOTP secret, and random Flask session key in
`./config/builder-auth.json`, with mode 600 inside the container. After
successful enrollment `/setup` permanently returns 404. Keep the `config`
directory during upgrades.

### Upgrade an existing Container Manager installation

1. Download the new release ZIP and stop the `docker-app-manager` project in
   Container Manager.
2. In File Station, make a safety copy of
   `/volume1/docker/docker-app-manager/config`. This directory contains the existing
   administrator and TOTP enrollment.
3. Extract the release and copy its files over
   `/volume1/docker/docker-app-manager`, replacing the application files. Do not
   delete or replace the existing `config` directory.
4. In **Container Manager → Project → docker-app-manager**, choose **Action → Build**
   (or recreate/update the project with the existing
   `docker-compose.container-manager.yml`) so the image is rebuilt from the
   new source.
5. Start the project and wait until the `docker-app-manager` container is healthy.
   Sign in with the existing administrator and TOTP code; `/setup` should not
   appear again.
6. Run the existing root DSM Task Scheduler task once, open **Backups**, and
   choose **Verify backup sources**. The dispatcher status should become
   active.

Rollback is file-based: stop the project, restore the previous release files
while keeping the same `config` directory, rebuild, and start it again. Project
data and backups below `/volume1/docker/<project>` and
`/volume1/docker/_BACKUPS` are not replaced by this Builder upgrade.

### Recover or reset Container Manager authentication

An upgrade preserves `config/builder-auth.json`, so the existing administrator
password and TOTP enrollment continue to work. If that file exists but has
invalid contents or unsafe permissions, Builder deliberately does not create a
new setup token. The browser and container log explain this state instead of
silently treating the installation as new.

First try preserving the account: stop the project, set `config` to mode 700
and `config/builder-auth.json` to mode 600, then restart it.

To explicitly replace the administrator and TOTP enrollment:

1. Stop the `docker-app-manager` project.
2. Create a temporary root task in DSM Task Scheduler with:
   `/bin/sh /volume1/docker/docker-app-manager/reset_setup_on_synology.sh --confirm-reset`
3. Run it once. The script moves the old auth file and TOTP replay state to
   `config/recovery-backups/<timestamp>/`, using private permissions.
4. Start the project, copy the new token from the container log, and complete
   `/setup`.
5. Remove the temporary DSM task after setup succeeds.

The reset requires the explicit `--confirm-reset` flag, refuses symbolic links
and unexpected install paths, and does not modify GLPI projects or backups.

If the fresh setup cannot be completed, keep the project stopped and restore
the previous account from the timestamped recovery directory. Move its
`builder-auth.json` back to `config/builder-auth.json` and, when present, move
`totp-replay-state` back to `/volume1/docker/.docker-app-manager-auth-state`. Set
`config` to mode 700 and both restored files to mode 600 before starting the
project. Do not overwrite a newly created authentication file: move that file
aside first so the reset remains reversible.

For HTTPS behind Synology Reverse Proxy, change
`BUILDER_SESSION_COOKIE_SECURE` to `true` in the project environment and
recreate the Builder container. Never expose port 5055 directly to the
internet.

## Routine administration

Check status and published port:

```sh
sudo docker ps --filter name=docker-app-manager
sudo docker port docker-app-manager
```

View logs:

```sh
sudo docker logs --tail 100 docker-app-manager
```

Stop and start the Builder:

```sh
sudo docker compose --project-name docker-app-manager --env-file .env -f docker-compose.app.yml stop
sudo docker compose --project-name docker-app-manager --env-file .env -f docker-compose.app.yml start
```

Test the health endpoint inside the container:

```sh
sudo docker exec docker-app-manager python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/healthz').read().decode())"
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
contains `glpi` (case-insensitive), including the managed project backup
folders. Backups for other applications may remain below `_BACKUPS`, but are
not offered by Docker App Manager.

## Scheduled Synology backup

The installer automatically copies the bundled script to:

```text
/volume1/docker/_BACKUPS/GLPI_backup/_system/GLPI_backup.sh
```

On the first installation, an existing unmanaged `GLPI_backup.sh` is preserved
once as `GLPI_backup.pre-builder.sh`. The managed script receives mode `750`.

Each managed project can have its own enabled/disabled schedule, frequency,
start time and retention period. The Builder stores those configurations
atomically below:

```text
/volume1/docker/_BACKUPS/GLPI_backup/_system/projects/<project>.env
```

Files receive mode `600`. Existing installations using the former single
`GLPI_backup.env` file are migrated automatically to a daily 02:00 schedule
without losing their selected project. The legacy file remains as a
compatibility fallback until the existing DSM task has been changed to the
dispatcher command.

Create one user-defined DSM Task Scheduler task, run it as root every five
minutes, and use:

```sh
/bin/bash /volume1/docker/_BACKUPS/Synology_task_scheduler/GLPI_backup_dispatcher.sh
```

The dispatcher checks which projects are due and runs them sequentially. It
writes heartbeat and per-project result state for the Builder UI. A separate
DSM task per project is not needed. Backups are kept in project-specific
folders below the backup root, preventing projects from mixing their sets.
The Builder creates `Synology_task_scheduler` automatically when it is missing
and atomically installs the dispatcher there whenever the Builder starts.
Project backup sets are stored below
`/volume1/docker/_BACKUPS/GLPI_backup/<project>/`. Internal schedules, state,
locks, and the managed backup script live separately below
`/volume1/docker/_BACKUPS/GLPI_backup/_system/`. Existing control files are
copied safely from the former `Restore_Scripts/GLPI` location when their new
equivalent does not exist; the old location is never deleted automatically.

The Builder creates a separate private MariaDB option file for every project
from that project's `.env`:

```text
/volume1/docker/_BACKUPS/GLPI_backup/_system/credentials/<project>.cnf
```

The credentials directory is mode `0700` and each option file is mode `0600`.
Saving or verifying an existing schedule migrates it away from the former
shared `GLPI_mysql_backup.cnf` automatically. This is required because projects
have independent MariaDB root passwords. Do not copy one project's option file
to another project.

When a project's backup configuration reports **Backup ready**, **Run backup
now** starts the managed backup script as a monitored background task. The
progress page shows the current phase, output, completion state, and action log.
Manual backups use the same global administration lock as restores, so two
mutating operations cannot run at the same time.

Each run creates a `GLPI_backup/<project>/YYYY-MM-DD_HHMMSS` directory containing:

- `database.sql.gz`;
- `files.tar.gz`;
- `manifest.json`, which identifies the project, creation time, and files;
- `SHA256SUMS` when `sha256sum` or `shasum` is available.

Sessions, cache, and temporary GLPI files are excluded. The script prevents
concurrent runs, publishes a backup only after full success, and by default
removes only timestamp-named backup sets older than the configured retention
period.

The dashboard attributes a new backup to a project only when `manifest.json`
matches that project. Existing `GLPI_Backup_*` sets remain readable for
compatibility.

## Rollback

After a successful installation, restore the Builder container preserved
immediately before it with:

```sh
cd /volume1/docker/docker-app-manager
sudo sh rollback_on_synology.sh
```

Rollback first starts the previous image as an isolated candidate with no
published port. It continues only when health succeeds, unauthenticated access
is denied, and `/login` is available. The current authenticated container is
not stopped until that proof passes and is preserved for automatic recovery if
the published rollback fails. Rollback does not reverse GLPI project changes,
recover deleted data, or restore the backup script or `.env`.

## Unified application and backup management

`Add application` is the single entry point for GLPI, n8n and Team Password
Manager. The visible flow is shared, while each application keeps its own
validated deployment and restore adapter. Legacy GLPI links remain supported.

The Backups workspace and one Synology dispatcher manage all three profiles:

- GLPI: MariaDB dump plus GLPI data and plugins;
- n8n: PostgreSQL dump plus its persistent `.n8n` data directory;
- Team Password Manager: MySQL dump plus its persistent application directory.

Each set contains an application-labelled manifest and SHA-256 checksums.
Secrets remain in the container/private `.env` and are not copied into the
schedule file or manifest.

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
| `BUILDER_AUTH_STATE_PATH` | `/volume1/docker/.docker-app-manager-auth-state` | Persistent mode-600 TOTP replay counter |
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
