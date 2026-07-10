# GLPI Project Builder Full Restore v10.4 - SSO cookie explicit, YAML locked

Deze versie bouwt verder op de werkende v7/v8-opbouw. De gegenereerde project-`docker-compose.yml` blijft bewust in dezelfde custom vorm staan, omdat die opbouw nodig was om het officiële GLPI-image stabiel te laten draaien op jouw Synology-opzet.

Belangrijk uitgangspunt:

```text
De v7 YAML-opbouw is leidend.
Alle nieuwe functies mogen die opbouw niet onnodig veranderen.
```

## Wat v10.4 doet

- Bestaande projecten automatisch tonen.
- Huidige poort, actieve Docker-poortmapping en containerstatus tonen.
- Bij nieuw project een hostpoort instellen.
- Achteraf de hostpoort aanpassen en alleen de GLPI-container opnieuw aanmaken.
- Vooraf controleren of de gekozen poort al door een andere Docker-container wordt gebruikt.
- Alleen de GLPI-container opnieuw aanmaken zonder database en GLPI-bestanden te raken.
- Bevestiging vragen bij acties die bestaande data kunnen overschrijven of verwijderen.
- Restore- en beheerlogs opslaan in `/volume1/docker/<project>/_builder_logs`.
- Zip/tar backups veiliger uitpakken: geen absolute paden, pad-traversal, symlinks, hardlinks of device-bestanden.
- Database-restore gebruikt `GLPI_DB_NAME` uit `.env`.
- HTML-output van logs en diagnose wordt escaped.
- POST-acties gebruiken CSRF-token.
- De builder-container start niet automatisch opnieuw: `--restart no` / `restart: "no"`.

## SSO-cookie-instellingen

Voor SSO is dit essentieel:

```ini
session.cookie_samesite = "Lax"
session.cookie_httponly = On
session.cookie_secure = Off
```

Nieuwe projecten krijgen standaard:

```env
GLPI_SESSION_COOKIE_SAMESITE=Lax
GLPI_SESSION_COOKIE_SECURE=Off
```

De GLPI-service krijgt deze waarden expliciet in de gegenereerde YAML:

```yaml
GLPI_SESSION_COOKIE_SAMESITE: ${GLPI_SESSION_COOKIE_SAMESITE}
GLPI_SESSION_COOKIE_SECURE: ${GLPI_SESSION_COOKIE_SECURE}
```

Bij elke GLPI-containerstart schrijft het bestaande custom entrypoint een PHP ini-bestand in de container en kopieert dat naar de aanwezige PHP `conf.d`-mappen. Er wordt dus geen extra host-volume toegevoegd. Dat houdt de YAML dichter bij de bewezen v7-opbouw.

Waarom niet meer via `/volume1/docker/<project>/php`?

```text
Dat werkte waarschijnlijk ook, maar het voegde een extra GLPI-volume toe.
Omdat jouw YAML gevoelig is, is v10.4 conservatiever:
de SSO-cookiefix blijft aanwezig, maar zonder extra mount.
```

## SameSite-keuzes

- `Lax`: standaard en bedoeld voor normale SSO-redirects.
- `Strict`: strenger, maar kan SSO breken.
- `None`: alleen gebruiken bij speciale flows. De app forceert dan automatisch `Cookie Secure = On`, omdat browsers `SameSite=None` normaal alleen met Secure/HTTPS accepteren.

`Cookie Secure` staat standaard op `Off`, omdat interne HTTP-toegang anders kan breken. Zet dit alleen op `On` als GLPI echt uitsluitend via HTTPS wordt geopend.

## Diagnose

De diagnose toont nu:

- waarden uit `.env`
- relevante container-environment
- poortmapping
- de PHP cookie override die bij containerstart wordt geschreven
- de effectieve PHP cookie-instellingen binnen de draaiende GLPI-container via `php -r` / `ini_get`
- de gegenereerde `docker-compose.yml`

Daarmee kun je na restore controleren of SSO-cookieinstellingen echt actief zijn.

## Installatie

Verwijder de oude buildermap en pak deze zip opnieuw uit:

```bash
sudo rm -rf /volume1/docker/glpi-project-builder-full-restore
sudo mkdir -p /volume1/docker/glpi-project-builder-full-restore
```

Pak de inhoud van deze zip uit in:

```text
/volume1/docker/glpi-project-builder-full-restore
```

Daarna:

```bash
cd /volume1/docker/glpi-project-builder-full-restore
sudo sh install_on_synology.sh
```

Open:

```text
http://<NAS-IP>:5055
```

## Testadvies

Test eerst met een testprojectnaam. Voor jouw SSO begin je met:

```text
Cookie SameSite: Lax
Cookie Secure: Off
```

Als GLPI alleen via HTTPS wordt geopend, kun je daarna eventueel `Cookie Secure: On` proberen.
