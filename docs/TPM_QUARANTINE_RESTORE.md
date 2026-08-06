# Team Password Manager quarantine restore

Docker App Manager accepts a database-only legacy `.sql`/`.sql.gz` dump for
inspection, but a complete backup set is cryptographically verified only when
the SQL file has a sibling `tpm-backup.json` manifest.

The official TPM backup procedure uses `mysqldump --hex-blob` and optionally
copies the installation-specific files. For a version-matched container the
database is sufficient for the vault data; an uploads archive can be included
when file attachments must also be tested.

Example manifest:

```json
{
  "schema": 1,
  "application": "teampasswordmanager",
  "application_version": "14.190.309",
  "database": {
    "file": "database.sql.gz",
    "sha256": "<sha256-of-database.sql.gz>",
    "engine": "mysql",
    "version": "5.7.44"
  },
  "tables": ["<every table recorded when the dump was created>"],
  "files": [
    {
      "role": "uploads",
      "file": "uploads.tar.gz",
      "sha256": "<sha256-of-uploads.tar.gz>"
    }
  ]
}
```

The manifest and every referenced file must be in the same backup-set folder
below the configured backup root. Paths outside that folder, symlinks,
unsupported file roles, checksum mismatches, missing declared tables, binary
dumps and server-level SQL statements are rejected.

Legacy SQL without a manifest remains usable for an isolated test, but the
quarantine report marks it `Database-only / legacy`; completeness cannot be
proven retroactively. It still requires a fixed TPM image tag matching the
operator-recorded source version.

Quarantine properties:

- a new database and new credentials;
- an internal Docker network and no additional application networks;
- binding to one management address (never `0.0.0.0` or `::`);
- no-new-privileges, PID and memory limits on the TPM application container;
- post-restore network and database-table proof;
- a private `quarantine-report.json` and a maximum 90-day expiry;
- expired environments cannot start or restart;
- archive stops containers and moves the full project into recoverable local
  archive storage instead of deleting it immediately.

This isolation blocks integrations by network policy. It does not remove or
anonymize passwords, SMTP settings, LDAP configuration, tokens or other
production data in the restored copy.
