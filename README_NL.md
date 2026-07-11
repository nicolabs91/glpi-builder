# GLPI Project Builder v11 voor Synology

Deze versie beheert interne GLPI Docker-projecten op een Synology RS822RP+.
De bewezen v7-opbouw van de gegenereerde `docker-compose.yml` is bewust
ongewijzigd. Broncode, uiteindelijke project-YAML en de compose van de Builder
worden afzonderlijk vergrendeld.

## Belangrijk beveiligingsmodel

Er is op verzoek geen app-login. Publiceer poort 5055 nooit op internet. Bind de
Builder standaard aan `127.0.0.1` of vul in `.env` expliciet het interne
beheer-IP van de NAS in. De Builder heeft via de Docker-socket verregaande
rechten. Start hem alleen voor beheer en stop hem daarna.

## Verbeteringen

- volledig Engelstalige gebruikersinterface en operationele meldingen;
- inhoudsafhankelijke, evenwichtige formulierbreedtes voor desktop en mobiel;
- echte restorevoortgang met actuele fase, percentage, verstreken tijd en activiteitenlog;
- Full restore als standaard met verplichte database- en GLPI-configback-up;
- expliciete Fresh installation voor een zeldzame volledig lege installatie;
- optioneel herstellen zonder plugins, marketplace-data en plugincache;
- restore-uitvoering in één bewaakte achtergrondtaak, zodat de voortgangspagina beschikbaar blijft;
- rustig projectdashboard en begeleide create/restore-wizard;
- server-side preflight met een apart uitvoerplan dat binnen tien minuten expliciet bevestigd moet worden;
- hercontrole van images, back-ups, poort en bestaand project vlak vóór uitvoering;
- bestaande projecten beheren vanuit één projectkaart;
- backupselectie uitsluitend onder de vaste `BACKUP_ROOT`;
- whitelists voor lokaal beschikbare GLPI- en database-images;
- één muterende beheeractie tegelijk;
- `/healthz`, beveiligingsheaders en browsercache uitgeschakeld;
- configureerbaar bind-IP, poort, tijdzone en imagebeleid;
- gecontroleerde installer met automatische rollback;
- onveranderlijk YAML-contract en statische securitytest.
- centraal `GLPI_backup.env` dat door de Builder naar het actuele productieproject wordt bijgewerkt;
- vast Synology-backupscript met locking, atomaire publicatie, checksums en 60 dagen retentie.

## Installatie

1. Plaats de map in `/volume1/docker/glpi-project-builder-v11`.
2. Kopieer `.env.example` naar `.env` of laat de installer dit doen.
3. Zet `BUILDER_BIND_IP` op het interne beheer-IP indien toegang vanaf een
   beheerpc nodig is. `127.0.0.1` is de veiligste default.
4. Installeer:

```sh
cd /volume1/docker/glpi-project-builder-v11
sudo sh install_on_synology.sh
```

Rollback:

```sh
sudo sh rollback_on_synology.sh
```

Stoppen na gebruik:

```sh
sudo docker stop glpi-project-builder-full-restore
```

## Synology Taakplanner-backup

Gebruik één keer deze vaste opdracht in Synology Taakplanner:

```sh
/bin/bash /volume1/docker/_BACKUPS/Restore_Scripts/GLPI/GLPI_backup.sh
```

Het script leest uitsluitend
`/volume1/docker/_BACKUPS/Restore_Scripts/GLPI/GLPI_backup.env`. Selecteer in
de Builder bij een bestaand project **Use for scheduled backups**, of laat bij
een nieuwe restore **Use this project for scheduled backups** aangevinkt. De
Builder schrijft dan automatisch de actuele projectmap, databasecontainer en
databasenaam. Het bestaande `GLPI_mysql_backup.cnf` blijft ongewijzigd.

Een bestaand onbeheerd `GLPI_backup.sh` wordt bij de eerste installatie bewaard
als `GLPI_backup.pre-builder.sh`.

## Tests

```sh
sh scripts/dev_loop.sh
```

Dit is de vaste kwaliteitslus: syntaxcontrole, exacte YAML-regressie,
composevalidatie, schone Docker-build, functionele tests en een echte
container-healthcheck. Zie `DEVELOPMENT_LOOP.md` voor de ontwikkel- en
releaseafspraken.

## Operationele aandachtspunten

- Maak back-ups van database, `/var/glpi`, plugins, `.env` en composebestanden.
- Bewaar minstens één back-up buiten dezelfde NAS.
- Controleer vrije schijfruimte en roteer containerlogs.
- Test grote restores eerst in een apart project.
- Upgrade MariaDB niet over grote versies zonder dump en hersteltest.
