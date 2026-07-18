# Contributing

## Branching Model

TeslaBuddy uses a simple trunk-based model with short-lived branches:

| Branch | Purpose |
|--------|---------|
| `main` | Production-ready code — protected, merge via PR only |
| `feature/<name>` | New features (e.g. `feature/add-horn-support`) |
| `fix/<name>` | Bug fixes (e.g. `fix/tls-port-default`) |

All work starts from `main` and is merged back via a Pull Request.

---

## Day-to-day Workflow

```
1. git checkout main && git pull
2. git checkout -b feature/my-thing
3. # … make changes, commit …
4. git push origin feature/my-thing
5. Open a Pull Request on GitHub
6. CI runs automatically — fix any lint/build errors
7. Merge PR into main once CI is green
```

---

## Automated CI (every push and PR)

The **CI** workflow (`.github/workflows/ci.yml`) runs on every push and on every
pull request, regardless of branch. It performs two checks:

| Check | What it does |
|-------|-------------|
| **Lint** | Runs `ruff check .` and `python -m py_compile` to catch errors early |
| **Docker build** | Builds the Docker image without pushing to verify the `Dockerfile` is valid |

> Both checks must be green before a PR can be merged.

---

## Preview Images

Pushing to a `feature/**` or `fix/**` branch automatically triggers the
**Preview Image** workflow (`.github/workflows/preview.yml`).

It builds a multi-arch image (`linux/amd64` + `linux/arm64`) and publishes it to
the GitHub Container Registry:

```
ghcr.io/gummigroda/teslabuddy:preview-<branch>-<short-sha>
```

**Example** — after pushing `feature/add-horn-support` with SHA `a1b2c3d`:
```
ghcr.io/gummigroda/teslabuddy:preview-feature-add-horn-support-a1b2c3d
```

You can pull and run this image to test your changes in a real environment before
the PR is merged:

```yaml
# docker-compose snippet for testing a preview image
  teslabuddy:
    image: ghcr.io/gummigroda/teslabuddy:preview-feature-add-horn-support-a1b2c3d
    ...
```

> Preview images are not cleaned up automatically. Delete old ones manually from
> the repository's **Packages** page on GitHub when no longer needed.

---

## Release Process

Releases follow [Semantic Versioning](https://semver.org/) (`vMAJOR.MINOR.PATCH`):

| Change type | Version bump |
|-------------|-------------|
| Breaking change / incompatible | MAJOR (`v2.0.0`) |
| New feature, backward-compatible | MINOR (`v1.3.0`) |
| Bug fix | PATCH (`v1.2.1`) |

### Steps

1. Ensure `main` is in the desired state (all PRs merged, CI green).

2. Create and push an annotated tag:
   ```bash
   git checkout main && git pull
   git tag -a v1.2.3 -m "Release v1.2.3"
   git push origin v1.2.3
   ```

3. The **Release** workflow (`.github/workflows/build-and-publish-image.yml`)
   triggers automatically. It builds a multi-arch image and pushes to:
   - **Docker Hub**: `gummigroda/teslabuddy`
   - **GHCR**: `ghcr.io/gummigroda/teslabuddy`

   with these tags automatically derived from the semver tag:

   | Tag | Example |
   |-----|---------|
   | Full version | `v1.2.3` |
   | Minor | `v1.2` |
   | Major | `v1` |
   | Latest | `latest` |

4. Optionally create a [GitHub Release](https://github.com/gummigroda/teslabuddy/releases/new)
   from the tag to add release notes / changelog.

### Required Repository Secrets

The release workflow needs these secrets configured under
**Settings → Secrets and variables → Actions**:

| Secret | Description |
|--------|-------------|
| `DOCKER_USERNAME` | Docker Hub username |
| `DOCKER_PASSWORD` | Docker Hub access token (not your account password) |

`GITHUB_TOKEN` is provided automatically by GitHub — no configuration needed.

---

## Local Development

```bash
# Install Python dependencies
pip install -r requirements.txt

# Run linter
pip install ruff
ruff check .

# Build Docker image locally
docker build -t teslabuddy .

# Run with env vars
docker run --rm \
  -e MQTT_HOST=mqtt.local \
  -e MQTT_TLS=true \
  -e DATABASE_HOST=postgres.local \
  -e DATABASE_USER=teslamate \
  -e DATABASE_PASS=secret \
  -e DATABASE_NAME=teslamate \
  teslabuddy
```

### Using Docker Secrets locally

For local testing, you can simulate Docker secrets by mounting files:
```bash
echo "mysecretpassword" > /tmp/mqtt_pass
docker run --rm \
  -e MQTT_PASS_FILE=/run/secrets/mqtt_pass \
  -v /tmp/mqtt_pass:/run/secrets/mqtt_pass:ro \
  ...
```
