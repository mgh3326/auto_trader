# Retired: Mac native HAProxy deployment

Native deployment was retired on 2026-09-03. Serving runs on NCP through [the NCP pull-deploy runbook](ncp-pull-deploy.md); `scripts/deploy-native.sh` and `ops/native/scripts/native_deploy_lib.sh` intentionally exit 70 for every invocation, and no launchd plist or Mac deployment workflow remains.
