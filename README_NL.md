# GLPI Project Builder v11 voor Synology

Deze versie beheert interne GLPI Docker-projecten op een Synology RS822RP+.
De bewezen v7-opbouw van de gegenereerde `docker-compose.yml` is bewust
ongewijzigd en wordt door `tests/test_yaml_contract.py` bewaakt.

## Belangrijk beveiligingsmodel

Er is op verzoek geen app-login. Publiceer poort 5055 nooit op internet. Bind de
Builder standaard aan `127.0.0.1` of vul in `.env` expliciet het interne
beheer-IP van de NAS in. De Builder heeft via de Docker-socket verregaande
rechten. Start hem alleen voor beheer en stop hem daarna.

## Verbeteringen

- rustig projectdashboard en begeleide create/restore-wizard;
- bestaande projecten beheren vanuit één projectkaart;
- backupselectie uitsluitend onder de vaste `BACKUP_ROOT`;
- whitelists voor lokaal beschikbare GLPI- en database-images;
- één muterende beheeractie tegelijk;
- `/healthz`, beveiligingsheaders en browsercache uitgeschakeld;
- configureerbaar bind-IP, poort, tijdzone en imagebeleid;
- gecontroleerde installer met automatische rollback;
- onveranderlijk YAML-contract en statische securitytest.

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

## Tests

```sh
python3 -m py_compile app.py
python3 tests/test_yaml_contract.py
python3 tests/test_static_security.py
```

## Operationele aandachtspunten

- Maak back-ups van database, `/var/glpi`, plugins, `.env` en composebestanden.
- Bewaar minstens één back-up buiten dezelfde NAS.
- Controleer vrije schijfruimte en roteer containerlogs.
- Test grote restores eerst in een apart project.
- Upgrade MariaDB niet over grote versies zonder dump en hersteltest.
