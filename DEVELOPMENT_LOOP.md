# Ontwikkelloop voor de GLPI Project Builder

## Niet-onderhandelbaar YAML-contract

De huidige YAML-opbouw is de bewezen basis voor de Synology RS822RP+. De
generator `write_compose`, het GLPI-entrypoint, de service- en containernamen,
volumes, poort `8080`, netwerken en `docker-compose.app.yml` worden niet
gewijzigd tijdens gewone app-ontwikkeling.

Het contract wordt op drie niveaus bewaakt:

1. een versie-onafhankelijke bronhash detecteert iedere wijziging aan
   `write_compose`;
2. de golden fixture vergelijkt de werkelijk gegenereerde project-YAML exact,
   byte voor byte;
3. een SHA-256-hash vergrendelt de compose van de Builder zelf.

Een noodzakelijke YAML-wijziging krijgt een afzonderlijke kandidaat-build en
vereist vooraf expliciete toestemming. De bestaande build blijft beschikbaar.
De kandidaat vervangt hem pas na een geslaagde create-, start-, herstart-,
restore- en rollbacktest op de RS822RP+.

## De vaste cyclus

1. Begin met een schone Git-status en beschrijf één kleine verbetering met
   concrete acceptatiecriteria.
2. Voeg eerst de passende regressietest toe. Raak de vergrendelde YAML niet aan.
3. Implementeer de kleinst mogelijke wijziging.
4. Draai `sh scripts/dev_loop.sh`. Dit controleert syntax, beide composevormen,
   bouwt schoon in Docker, voert alle tests uit en wacht op een echte healthy
   Builder-container.
5. Controleer de diff expliciet. Een onverwachte wijziging aan de YAML, de
   generator of het entrypoint stopt de ronde.
6. Test de gewijzigde gebruikersflow, inclusief foutpad en herhaalde actie.
7. Maak pas daarna een release-zip en SHA-256-checksum. Bewaar de vorige zip als
   rollbackversie.
8. Installeer eerst als kandidaat, voer de NAS-smoketest uit en promoveer alleen
   een volledig geslaagde kandidaat.

## Definitie van klaar

Een wijziging is alleen klaar als alle automatische controles groen zijn, de
container `healthy` wordt, de UI-flow en het foutpad werken, de diff geen
onbedoelde YAML-wijziging bevat, de release reproduceerbaar is en rollback
beschikbaar blijft.
