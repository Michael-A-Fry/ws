# Verify a release

Check that a ws release file is the one the project built, before you install it.

Every release tag (`vX.Y.Z`) builds a source archive and a wheel in GitHub Actions. Each file comes with a checksum, a
build provenance attestation and a cosign signature. A software bill of materials (SBOM) in CycloneDX form lists what
went in.

## Before you start

- A downloaded release file, for example `ws_aitoolkit-X.Y.Z-py3-none-any.whl`. Download it from the
  [releases page](https://github.com/Michael-A-Fry/ws/releases) with its `.sha256` file and its `.sigstore.json` file.
- For step 2: the [GitHub CLI](https://cli.github.com/) (`gh`).
- For step 3: [cosign](https://docs.sigstore.dev/cosign/system_config/installation/).

## Steps

1. Check the checksum.

   ```sh
   sha256sum -c ws_aitoolkit-X.Y.Z-py3-none-any.whl.sha256
   ```

   On macOS, use `shasum -a 256 -c` in place of `sha256sum -c`.

2. Check the build provenance. It proves that the file came from this repository's release workflow.

   ```sh
   gh attestation verify ws_aitoolkit-X.Y.Z-py3-none-any.whl --repo Michael-A-Fry/ws
   ```

3. Check the cosign signature. It proves that the same workflow signed the file.

   ```sh
   cosign verify-blob \
     --bundle ws_aitoolkit-X.Y.Z-py3-none-any.whl.sigstore.json \
     --certificate-identity-regexp '^https://github.com/Michael-A-Fry/ws/.github/workflows/release.yml@refs/tags/v' \
     --certificate-oidc-issuer https://token.actions.githubusercontent.com \
     ws_aitoolkit-X.Y.Z-py3-none-any.whl
   ```

4. Install the file you checked.

   ```sh
   uv tool install ./ws_aitoolkit-X.Y.Z-py3-none-any.whl
   ```

## Success

Step 1 prints `OK`. Step 2 prints `Loaded digest` and `Verification succeeded`. Step 3 prints `Verified OK`. A failure at
any step exits with a non-zero code. Don't install the file.

## What the installer checks for you

`install.sh` with no `--version` option does step 1 itself, and step 3 when `cosign` is on your `PATH`. It refuses to
install when a check fails. It doesn't run the attestation check, so run step 2 yourself if you want it.

## Limits

- The checks prove where a file came from. They don't prove that the code is free of bugs.
- Releases are immutable. A tag can't be moved or deleted after it's published.
- The SBOM lists the dependencies. ws's core has none outside the Python standard library.
