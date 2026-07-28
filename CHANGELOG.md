# Changelog

## 0.3.2

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
