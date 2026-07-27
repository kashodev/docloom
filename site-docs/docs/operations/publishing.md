# Publishing to PyPI

docsynth publishes to [PyPI](https://pypi.org/project/docsynth/) via **Trusted
Publishing** — GitHub Actions mints a short-lived OIDC token that PyPI verifies,
so there is **no API token** stored in the repo or in GitHub secrets. The
[`release.yml`](https://github.com/kashodev/docsynth/blob/main/.github/workflows/release.yml)
workflow does the work; you cut a GitHub Release and it builds and uploads.

## One-time setup

Two things, done once.

### 1. Trusted publisher on PyPI

The `docsynth` project already exists (a `0.0.0` placeholder reserved the name),
so add the publisher to the existing project:

- **pypi.org → Your projects → `docsynth` → Manage → Publishing → Add a trusted
  publisher** (GitHub tab):

    | Field | Value |
    |---|---|
    | Owner | `kashodev` |
    | Repository | `docsynth` |
    | Workflow name | `release.yml` |
    | Environment | `pypi` |

### 2. A `pypi` environment on GitHub

`release.yml`'s publish job runs in an environment named `pypi` (which the
publisher above is scoped to):

- **GitHub → Settings → Environments → New environment → `pypi`.**
- Recommended protection: **Deployment branches and tags → restrict to tags**
  matching `v*`, so a publish can only originate from a release tag.

## Cut a release

1. **Bump the version** in `pyproject.toml` (`[project].version`) on a branch and
   merge it (CI must pass). Follow SemVer: `0.1.0`, `0.1.1`, `0.2.0`, …
2. **Tag + release.** Create a GitHub Release whose tag is `v<version>` (e.g.
   `v0.1.0`) targeting `main`. Publishing the release triggers `release.yml`.

    ```bash
    # tag matches [project].version, prefixed with v
    git tag v0.1.0 && git push origin v0.1.0
    # then: GitHub → Releases → Draft a new release → pick the tag → Publish
    ```

3. The workflow **verifies the tag matches `[project].version`**, builds the
   sdist + wheel, runs `twine check`, and uploads to PyPI via OIDC.
4. **Verify:** the new version appears at
   [pypi.org/project/docsynth](https://pypi.org/project/docsynth/), and
   `pip install docsynth` installs it.

!!! note "First real release"
    `[project].version` is already `0.1.0`, so the first release tag is `v0.1.0`.
    The `0.0.0` placeholder can be left as-is or yanked
    (`Manage → Releases → 0.0.0 → Yank`) — yanking hides it from resolvers
    without deleting the name.

## Test the build before releasing

The build is fully reproducible locally — no need to release to check it:

```bash
pip install build twine
python -m build            # -> dist/*.whl and dist/*.tar.gz
twine check dist/*         # metadata/README render check
```

For an end-to-end dry run, add a **TestPyPI** trusted publisher (same fields, on
[test.pypi.org](https://test.pypi.org/)) and publish a pre-release tag to it
first; the `docsynth` name is free there.

## After the first PyPI release

Flip the [Quickstart](../quickstart.md) and the README install to **PyPI-first**
(`pip install 'docsynth[studio]'`) now that it resolves to a real package.
