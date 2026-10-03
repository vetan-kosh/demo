# VetanKosh Dependency Policy

Runtime dependencies are pinned to an explicit tested baseline. Production upgrades must be intentional: create a staging environment, install the proposed versions, run compile/import/migration/E2E tests, run a vulnerability audit (for example `python -m pip install pip-audit && pip-audit -r backend/requirements.txt`), then deploy. Never auto-upgrade production dependencies without tests.

`requirements.txt` pins direct runtime dependencies. Transitive dependencies should be captured with a lock/constraints file by the deployment CI for fully reproducible builds. Security fixes may require moving off a pinned version; pinning is for reproducibility, not a reason to ignore advisories.
