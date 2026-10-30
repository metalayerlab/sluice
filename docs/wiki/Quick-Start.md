# Quick Start

From nothing to a working sluice in a few minutes. Everything else is in [In-Depth Configuration](In-Depth-Configuration).

## Prerequisites

- For running from source: Python >= 3.13 and [uv](https://docs.astral.sh/uv/)
<!-- guidance: add what it talks to: accounts, API keys, other services. -->

## Install

```sh
uv tool install git+https://github.com/metalayerlab/sluice
```

## First run

```sh
sluice --help
sluice config      # the effective configuration, and where each value came from
```

<!-- guidance: extend this into the first real task a user does, with the expected output. -->

## Next steps

- [In-Depth Configuration](In-Depth-Configuration): every setting
- [Troubleshooting](Troubleshooting) if something doesn't work
