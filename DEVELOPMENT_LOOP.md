# Development loop for Docker App Manager

## Non-negotiable YAML contract

The current YAML structure is the proven baseline for the Synology RS822RP+.
Normal application development must not change the `write_compose` generator,
the GLPI entrypoint, service or container names, volumes, internal port `8080`,
networks, or `docker-compose.app.yml`.

The contract is protected at three levels:

1. a version-independent source hash detects every change to `write_compose`;
2. a golden fixture compares the generated project YAML byte for byte;
3. a SHA-256 hash locks the Builder Compose file.

A necessary YAML change requires a separate candidate build and explicit prior
approval. The existing build remains available. A candidate may replace it only
after successful create, start, restart, restore, and rollback tests on the
RS822RP+.

## Required cycle

1. Start with a clean Git status and define one small improvement with concrete
   acceptance criteria.
2. Add the appropriate regression test first. Do not touch locked YAML.
3. Implement the smallest possible change.
4. Run `sh scripts/dev_loop.sh`. It checks syntax and both Compose forms, builds
   a clean Docker image, automatically discovers every `test_*.py` test, checks
   installed dependencies, and waits for a healthy Builder container.
5. Review the diff explicitly. Any unexpected change to YAML, the generator, or
   the entrypoint stops the cycle.
6. Test the changed user flow, including its failure path and repeated use.
7. Only then create a release zip and SHA-256 checksum. Keep the previous zip as
   the rollback release.
8. Install the new build as a candidate first, run the NAS smoke test, and
   promote only a fully successful candidate.

## Definition of done

A change is complete only when every automated check passes, the container
becomes `healthy`, the UI flow and failure path work, the diff contains no
unintended YAML changes, the release is reproducible, and rollback remains
available.
