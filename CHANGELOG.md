# Changelog

## 0.5.0-rc.7

- Show TPM and n8n deployments on the same live, stage-by-stage progress page
  used by GLPI instead of blocking on the confirmation request.
- Scan the complete configured backup root before applying application-specific
  filtering, so TPM backups stored outside GLPI-named folders are selectable.
- Keep GLPI, n8n and TPM restore choices isolated from each other and add
  regression coverage for non-GLPI TPM backup folder layouts.

## 0.5.0-rc.6

- Generate the n8n PostgreSQL data mount according to the selected local image:
  PostgreSQL 18 and newer mount the persistent directory at
  `/var/lib/postgresql`, while PostgreSQL 17 and older retain
  `/var/lib/postgresql/data`.
- Add regression coverage for both PostgreSQL storage layouts.

## 0.5.0-rc.5

- Make newly created n8n and TPM bind-mount directories writable for their
  non-root application and database users during first-time initialization on
  Synology.
- Include bounded, password-redacted database-container logs when deployment
  fails because the database remains unhealthy.

## 0.5.0-rc.4

- Filter isolated-restore backup choices by application manifest so n8n and
  Team Password Manager no longer offer GLPI or other-application backups.
- Keep explicitly named legacy TPM candidates visible while excluding generic
  unclassified SQL files.

## 0.5.0-rc.3

- Allow independent selection of locally installed compatible application and
  database images for isolated compatibility tests.
- Treat backup versions as source metadata rather than requiring them to match
  the selected target images.
- Remove the manually entered backup application version from isolated restore.
- Enable GLPI isolated test restore for manifest/checksum-verified database and
  files sets, using new credentials and a new internal Docker network.
- Verify GLPI containers, network attachment, rewritten database configuration
  and restored table count, then write a content-free proof report.
- Record application and database image versions in new GLPI and TPM backup
  manifests.

## 0.5.0-rc.2

- List only locally installed, allowlisted n8n and Team Password Manager
  application images in the wizard.
- Show whether the required PostgreSQL or MySQL image is installed and block
  deployment instead of automatically pulling a missing image.

## 0.5.0-rc.1

Test prerelease. TPM and n8n isolated restores are available for NAS proof
testing. GLPI isolated restore remains visible but disabled until its adapter
is completed and verified.

- Unify GLPI, n8n and Team Password Manager under the **Add application** flow
  while retaining legacy GLPI route compatibility.
- Expand Overview and Backups from GLPI-only wording and schedules to
  application-aware inventory and management.
- Add scheduled and manual backup adapters for GLPI, n8n/PostgreSQL and Team
  Password Manager/MySQL using one dispatcher, per-app manifests and checksums.
- Use one **Add application** entry point for GLPI, n8n, Team Password Manager
  and future profiles, while retaining GLPI's established advanced wizard.
- Add checksum-backed TPM backup-set manifests, optional uploads archives,
  source/table inventory, post-restore proof reports and fail-closed mismatch
  handling.
- Restrict quarantine web access to one management address, add resource and
  privilege limits, enforce expiry, block expired starts and provide
  recoverable archive cleanup.
- Add Synology preflight checks and pre-write image/platform verification.
- Add an **Isolated test restore** mode and fail-closed adapter contract per
  application profile.
- Support TPM `.sql` and `.sql.gz` test restores only when the recorded backup
  version exactly matches a fixed TPM image tag.
- Restore into a newly generated MySQL database on an internal Docker network
  without external routes; production database credentials are never reused.
- Reject binary dumps, server-level SQL statements, unsupported profiles and
  unknown or mismatched versions before the application starts.
- Mark restored projects as **TEST / QUARANTINE** and prevent in-place image
  updates that would invalidate the version contract.
- Present backup intervals as days, weeks or months while retaining compatible
  hour values in schedule configuration files.

## 0.4.1

- Rename the Synology application directory, Compose project, service,
  container, image and authentication replay-state file to
  `docker-app-manager`.
- Add a guarded migration script that copies the existing administrator
  configuration and TOTP replay state from a `glpi-builder` installation,
  stops the legacy service, and starts the renamed application.
- Preserve the complete legacy installation directory and pre-upgrade
  container until the administrator has verified login, applications and
  backups, providing a recoverable rollback path.

## 0.4.0

- Rename the product interface and documentation to Docker App Manager while
  initially retaining the existing `glpi-builder` repository and NAS identity,
  as well as the authentication state and GLPI project contract.
- Add an extensible, fail-closed application profile catalog with explicit
  `.builder-app.json` ownership manifests.
- Add review-first fresh deployment for n8n with PostgreSQL and Team Password
  Manager with the vendor-documented MySQL 5.7 database.
- Generate private per-project secrets, isolated networks and persistent bind
  mounts, with application-specific image allowlists and health checks.
- Add application status, sanitized Compose views and start, stop, restart and
  pull-and-apply update actions.
- Reserve ports from both legacy GLPI projects and new profile-managed apps,
  and refuse to adopt or overwrite unrelated Compose directories.
- Keep automated n8n and Team Password Manager backup/restore and arbitrary
  custom Compose import disabled until application-specific recovery tests are
  proven.
- Expand the regression suite to cover profiles, private files, ownership,
  lifecycle actions and route authorization.

## 0.3.2

- Recognize ISO 8601 backup-manifest timestamps with `T`, `Z`, and numeric
  timezone offsets so valid backups no longer show `Unknown age`.
- Preserve existing Builder credentials and TOTP enrollment during normal
  Container Manager upgrades.
- Explain invalid authentication-file states in the browser and container log,
  including why no new setup token is generated.
- Add an explicit Synology authentication reset script that privately backs up
  the current auth file and TOTP replay state before allowing fresh setup.
- Enforce private permissions when creating the authentication directory and
  add regression coverage for recovery behavior.

## 0.3.1

- Harden restore and backup directory permissions.
- Improve Synology backup dispatcher detection.
